# ============================================================
# strategy/engine.py
#
# Strategy Engine
# ──────────────────────────────────────────────────────────
# Combines market structure, technical indicators, liquidity
# analysis, and ML probability into a single confluence score
# and generates probabilistic trade signals.
#
# SIGNAL LIFECYCLE:
#   Raw inputs → Confluence scoring → Filter → TradeSignal
#
# CONFLUENCE SCORING MODEL:
#   Each factor contributes a weighted score (0-1).
#   Final score = Σ(weight_i × factor_score_i) × 100
#   Signal emitted only if score ≥ min_confidence_score
# ============================================================

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple
import uuid

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import CONFIG
from core.market_structure import (
    MarketStructureEngine, StructureAnalysis,
    Trend, SignalType, FairValueGap, LiquidityZone
)


# ────────────────────────────────────────────────
# DATA CLASSES
# ────────────────────────────────────────────────

@dataclass
class TradeSignal:
    """
    A fully specified trade signal with entry parameters,
    confidence score, and reasoning trace.
    """
    signal_id: str
    symbol: str
    timeframe: str
    direction: SignalType
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float           # 0-100
    risk_reward: float

    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Confluence breakdown (for explainability)
    confluence_scores: dict = field(default_factory=dict)
    reasoning: List[str] = field(default_factory=list)

    # Position sizing
    risk_pct: float = 1.0
    pip_risk: float = 0.0

    # Optional ML score
    ml_probability: float = 0.0
    sentiment_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "entry": round(self.entry_price, 5),
            "sl": round(self.stop_loss, 5),
            "tp": round(self.take_profit, 5),
            "confidence": round(self.confidence, 1),
            "rr": round(self.risk_reward, 2),
            "ml_prob": round(self.ml_probability, 3),
            "sentiment": round(self.sentiment_score, 3),
            "reasoning": self.reasoning,
            "timestamp": self.timestamp.isoformat(),
        }

    def __str__(self) -> str:
        return (
            f"[{self.direction.value}] {self.symbol} | "
            f"Entry: {self.entry_price:.5f} | "
            f"SL: {self.stop_loss:.5f} | "
            f"TP: {self.take_profit:.5f} | "
            f"Confidence: {self.confidence:.1f}% | "
            f"R:R {self.risk_reward:.1f}"
        )


@dataclass
class IndicatorSet:
    """Technical indicators computed for the current bar."""
    rsi_14: float = 50.0
    atr_14: float = 0.001
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0
    adx_14: float = 25.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_mid: float = 0.0
    volume_ratio: float = 1.0   # Current volume / 20-bar average volume


# ────────────────────────────────────────────────
# INDICATOR CALCULATOR
# ────────────────────────────────────────────────

class IndicatorCalculator:
    """
    Lightweight indicator calculator using pure pandas/numpy.
    Does not depend on TA-Lib (no C compilation required).
    """

    @staticmethod
    def compute_all(df: pd.DataFrame) -> IndicatorSet:
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        vol   = df["volume"]

        return IndicatorSet(
            rsi_14=IndicatorCalculator._rsi(close, 14),
            atr_14=IndicatorCalculator._atr(high, low, close, 14),
            ema_20=IndicatorCalculator._ema(close, 20),
            ema_50=IndicatorCalculator._ema(close, 50),
            ema_200=IndicatorCalculator._ema(close, 200),
            adx_14=IndicatorCalculator._adx(high, low, close, 14),
            **IndicatorCalculator._bollinger(close, 20, 2),
            volume_ratio=IndicatorCalculator._volume_ratio(vol, 20),
        )

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> float:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / (loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    @staticmethod
    def _ema(close: pd.Series, period: int) -> float:
        if len(close) < period:
            return float(close.iloc[-1])
        return float(close.ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    @staticmethod
    def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
        """Simplified ADX — measures trend strength (0-100, >25 = trending)."""
        up_move   = high.diff()
        down_move = -low.diff()
        plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0)
        minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0)
        tr = pd.concat([high - low, (high - close.shift()).abs(),
                        (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        plus_di  = 100 * plus_dm.rolling(period).mean() / (atr + 1e-10)
        minus_di = 100 * minus_dm.rolling(period).mean() / (atr + 1e-10)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
        return float(dx.rolling(period).mean().iloc[-1])

    @staticmethod
    def _bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> dict:
        mid = close.rolling(period).mean()
        std = close.rolling(period).std()
        return {
            "bb_upper": float((mid + std_mult * std).iloc[-1]),
            "bb_lower": float((mid - std_mult * std).iloc[-1]),
            "bb_mid": float(mid.iloc[-1]),
        }

    @staticmethod
    def _volume_ratio(volume: pd.Series, period: int = 20) -> float:
        avg = volume.rolling(period).mean().iloc[-1]
        return float(volume.iloc[-1] / (avg + 1e-10))


# ────────────────────────────────────────────────
# STRATEGY ENGINE
# ────────────────────────────────────────────────

class StrategyEngine:
    """
    Produces TradeSignals by scoring confluence across 6 factors.

    SCORING WEIGHTS (configurable in settings.py):
      market_structure   35%  — BOS/CHoCH + trend alignment
      fvg_present        20%  — FVG exists at/near entry zone
      liquidity_sweep    15%  — Recent liquidity grab (stop hunt)
      htf_trend_aligned  15%  — Higher-timeframe trend matches signal
      sentiment_score    10%  — News/macro sentiment
      ml_probability      5%  — ML model confidence (grows as model matures)

    The weights reflect that price action and structure are the
    primary edge; ML and sentiment are supplemental.
    """

    def __init__(self):
        self.cfg = CONFIG.strategy
        self.struct_engine = MarketStructureEngine()
        self.indicator_calc = IndicatorCalculator()
        self._signal_history: List[TradeSignal] = []

    def generate_signal(
        self,
        symbol: str,
        primary_analysis: StructureAnalysis,
        htf_analysis: StructureAnalysis,
        df_primary: pd.DataFrame,
        ml_probability: float = 0.5,
        sentiment_score: float = 0.0,  # -1 to +1
    ) -> Optional[TradeSignal]:
        """
        Main entry point. Returns a TradeSignal if confluence is
        above the threshold, or None if no trade setup exists.
        """
        indicators = self.indicator_calc.compute_all(df_primary)
        current_price = primary_analysis.current_price

        # Step 1: Determine candidate direction from structure
        candidate = self._get_candidate_direction(primary_analysis)
        if candidate == SignalType.NONE:
            return None

        # Step 2: Score each confluence factor
        scores = self._score_confluence(
            direction=candidate,
            primary=primary_analysis,
            htf=htf_analysis,
            indicators=indicators,
            ml_prob=ml_probability,
            sentiment=sentiment_score,
        )

        # Step 3: Compute weighted confidence
        weights = self.cfg.confluence_weights
        confidence = sum(
            weights.get(k, 0) * v for k, v in scores.items()
        ) * 100

        reasoning = self._build_reasoning(candidate, scores, indicators, primary_analysis)

        logger.debug(f"{symbol} {candidate.value} confidence: {confidence:.1f}%")

        # Step 4: Filter below threshold
        if confidence < self.cfg.min_confidence_score:
            return None

        # Step 5: Calculate entry parameters
        entry, sl, tp = self._calculate_entry_params(
            direction=candidate,
            analysis=primary_analysis,
            indicators=indicators,
            df=df_primary,
        )

        if entry is None:
            return None

        rr = abs(tp - entry) / (abs(entry - sl) + 1e-10)

        # Enforce minimum R:R
        if rr < self.cfg.risk_reward_min:
            logger.debug(f"Signal rejected: R:R {rr:.2f} < {self.cfg.risk_reward_min}")
            return None

        signal = TradeSignal(
            signal_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            timeframe=primary_analysis.timeframe,
            direction=candidate,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            risk_reward=rr,
            confluence_scores=scores,
            reasoning=reasoning,
            ml_probability=ml_probability,
            sentiment_score=sentiment_score,
            pip_risk=abs(entry - sl),
        )

        self._signal_history.append(signal)
        logger.info(f"Signal generated: {signal}")
        return signal

    # ─── DIRECTION DETECTION ──────────────────────────────

    def _get_candidate_direction(self, analysis: StructureAnalysis) -> SignalType:
        """
        Prioritise CHoCH (reversal) over BOS (continuation).
        CHoCH signals higher probability setups.

        Logic:
        - If last event is CHoCH → trade the reversal direction
        - If last event is BOS   → trade in BOS direction (continuation)
        - If trend is clear with no recent BOS/CHoCH → skip (wait for confirmation)
        """
        last_choch = analysis.last_choch
        last_bos   = analysis.last_bos

        # Prefer most recent event
        if last_choch and last_bos:
            if last_choch.bar_index > last_bos.bar_index:
                return (SignalType.BUY if last_choch.direction == Trend.BULLISH
                        else SignalType.SELL)
            else:
                return (SignalType.BUY if last_bos.direction == Trend.BULLISH
                        else SignalType.SELL)
        elif last_choch:
            return SignalType.BUY if last_choch.direction == Trend.BULLISH else SignalType.SELL
        elif last_bos:
            return SignalType.BUY if last_bos.direction == Trend.BULLISH else SignalType.SELL
        else:
            return SignalType.NONE

    # ─── CONFLUENCE SCORING ───────────────────────────────

    def _score_confluence(
        self,
        direction: SignalType,
        primary: StructureAnalysis,
        htf: StructureAnalysis,
        indicators: IndicatorSet,
        ml_prob: float,
        sentiment: float,
    ) -> dict:
        """
        Returns scores dict: each value in [0.0, 1.0].
        Higher = stronger confluence for the given direction.
        """
        scores = {}

        # 1. MARKET STRUCTURE SCORE (0-1)
        # Rewards: recent BOS + CHoCH in the same direction
        struct_score = 0.0
        if direction == SignalType.BUY:
            if primary.last_bos and primary.last_bos.direction == Trend.BULLISH:
                struct_score += 0.5
            if primary.last_choch and primary.last_choch.direction == Trend.BULLISH:
                struct_score += 0.5
            if primary.trend == Trend.BULLISH:
                struct_score = min(struct_score + 0.2, 1.0)
        else:  # SELL
            if primary.last_bos and primary.last_bos.direction == Trend.BEARISH:
                struct_score += 0.5
            if primary.last_choch and primary.last_choch.direction == Trend.BEARISH:
                struct_score += 0.5
            if primary.trend == Trend.BEARISH:
                struct_score = min(struct_score + 0.2, 1.0)
        scores["market_structure"] = min(struct_score, 1.0)

        # 2. FVG SCORE (0-1)
        # Does an unfilled FVG exist in the entry zone direction?
        fvg_direction = Trend.BULLISH if direction == SignalType.BUY else Trend.BEARISH
        relevant_fvgs = [f for f in primary.fvgs if f.direction == fvg_direction and not f.filled]
        if relevant_fvgs:
            # Score higher if price is close to the FVG
            nearest_fvg = min(relevant_fvgs, key=lambda f: abs(
                (f.gap_top + f.gap_bottom) / 2 - primary.current_price
            ))
            fvg_mid = (nearest_fvg.gap_top + nearest_fvg.gap_bottom) / 2
            proximity = 1 - min(abs(primary.current_price - fvg_mid) / (fvg_mid + 1e-10) * 100, 1)
            scores["fvg_present"] = max(0.3, proximity)  # At least 0.3 if FVG exists
        else:
            scores["fvg_present"] = 0.0

        # 3. LIQUIDITY SWEEP SCORE (0-1)
        # Was a liquidity zone swept recently (stop hunt before the move)?
        swept_zones = [z for z in primary.liquidity_zones if z.swept]
        if direction == SignalType.BUY and swept_zones:
            # Buy after a sweep of equal lows (bearish stop hunt)
            eq_lows_swept = [z for z in swept_zones if z.zone_type in ("equal_lows", "previous_low")]
            scores["liquidity_sweep"] = min(len(eq_lows_swept) * 0.5, 1.0)
        elif direction == SignalType.SELL and swept_zones:
            eq_highs_swept = [z for z in swept_zones if z.zone_type in ("equal_highs", "previous_high")]
            scores["liquidity_sweep"] = min(len(eq_highs_swept) * 0.5, 1.0)
        else:
            scores["liquidity_sweep"] = 0.0

        # 4. HTF TREND ALIGNMENT SCORE (0-1)
        # Is the higher-timeframe trend aligned with our signal direction?
        if direction == SignalType.BUY and htf.trend == Trend.BULLISH:
            scores["htf_trend_aligned"] = 1.0
        elif direction == SignalType.SELL and htf.trend == Trend.BEARISH:
            scores["htf_trend_aligned"] = 1.0
        elif htf.trend == Trend.RANGING:
            scores["htf_trend_aligned"] = 0.5   # Neutral — no bias
        else:
            scores["htf_trend_aligned"] = 0.0   # Against HTF trend — heavily penalized

        # 5. SENTIMENT SCORE (0-1)
        # Convert -1..+1 sentiment to 0..1 score in the signal direction
        if direction == SignalType.BUY:
            scores["sentiment_score"] = (sentiment + 1) / 2     # Map [-1,1] → [0,1]
        else:
            scores["sentiment_score"] = (1 - sentiment) / 2

        # 6. ML PROBABILITY SCORE (0-1)
        # Directly from the ML model (already 0-1)
        if direction == SignalType.BUY:
            scores["ml_probability"] = max(0.0, min(ml_prob, 1.0))
        else:
            scores["ml_probability"] = max(0.0, min(1 - ml_prob, 1.0))

        return scores

    # ─── ENTRY PARAMETERS ─────────────────────────────────

    def _calculate_entry_params(
        self,
        direction: SignalType,
        analysis: StructureAnalysis,
        indicators: IndicatorSet,
        df: pd.DataFrame,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Calculates entry, stop-loss, and take-profit prices.

        ENTRY:  Current market close (or FVG midpoint for limit entries)
        STOP:   Below nearest swing low (BUY) / above nearest swing high (SELL)
                Padded by 0.5 × ATR to account for spread and slippage
        TARGET: Risk × target R:R ratio from the nearest liquidity zone
                or structural resistance/support level
        """
        current = analysis.current_price
        atr = indicators.atr_14
        pad = atr * 0.5

        if direction == SignalType.BUY:
            # Entry at current price (market order at signal bar)
            entry = current

            # Stop-loss: below the most recent swing low
            if analysis.swing_lows:
                nearest_low = min(
                    analysis.swing_lows,
                    key=lambda s: abs(s.price - current)
                )
                sl = nearest_low.price - pad
            else:
                sl = current - atr * 2   # Fallback: 2 ATR below

            # Take-profit: nearest resistance level (swing high above current)
            highs_above = [s for s in analysis.swing_highs if s.price > current]
            if highs_above:
                nearest_resistance = min(highs_above, key=lambda s: s.price)
                tp = nearest_resistance.price - pad
            else:
                # If no resistance, use R:R multiple
                tp = entry + abs(entry - sl) * self.cfg.risk_reward_min

        else:  # SELL
            entry = current

            # Stop-loss: above the most recent swing high
            if analysis.swing_highs:
                nearest_high = min(
                    analysis.swing_highs,
                    key=lambda s: abs(s.price - current)
                )
                sl = nearest_high.price + pad
            else:
                sl = current + atr * 2

            # Take-profit: nearest support level (swing low below current)
            lows_below = [s for s in analysis.swing_lows if s.price < current]
            if lows_below:
                nearest_support = max(lows_below, key=lambda s: s.price)
                # Ensure TP is meaningfully below entry (at least 1 ATR)
                tp = min(nearest_support.price + pad, current - atr)
            else:
                tp = entry - abs(sl - entry) * self.cfg.risk_reward_min

        # Validate: ensure SL and TP are on correct sides
        if direction == SignalType.BUY and (sl >= entry or tp <= entry):
            logger.warning(f"Invalid BUY params: entry={entry} sl={sl} tp={tp}")
            return None, None, None
        if direction == SignalType.SELL and (sl <= entry or tp >= entry):
            logger.warning(f"Invalid SELL params: entry={entry} sl={sl} tp={tp}")
            return None, None, None

        return entry, sl, tp

    # ─── REASONING BUILDER ────────────────────────────────

    def _build_reasoning(
        self,
        direction: SignalType,
        scores: dict,
        indicators: IndicatorSet,
        analysis: StructureAnalysis,
    ) -> List[str]:
        """
        Generates human-readable explanation of the signal.
        Every signal must be explainable — this is non-negotiable.
        """
        reasons = []

        if scores.get("market_structure", 0) > 0.5:
            event = "CHoCH" if analysis.last_choch else "BOS"
            reasons.append(f"✓ {event} detected in {direction.value} direction on {analysis.timeframe}")

        if scores.get("fvg_present", 0) > 0.3:
            reasons.append(f"✓ Unfilled {'bullish' if direction==SignalType.BUY else 'bearish'} FVG present near entry")

        if scores.get("liquidity_sweep", 0) > 0.3:
            zone_type = "equal lows" if direction == SignalType.BUY else "equal highs"
            reasons.append(f"✓ Liquidity sweep of {zone_type} detected (stop hunt complete)")

        if scores.get("htf_trend_aligned", 0) == 1.0:
            reasons.append(f"✓ Higher-timeframe trend aligned: {analysis.trend.value}")
        elif scores.get("htf_trend_aligned", 0) == 0.0:
            reasons.append(f"⚠ Against higher-timeframe trend — reduced confidence")

        if scores.get("sentiment_score", 0) > 0.6:
            reasons.append(f"✓ Macro sentiment supports {direction.value}")
        elif scores.get("sentiment_score", 0) < 0.4:
            reasons.append(f"⚠ Macro sentiment neutral or against signal")

        if indicators.adx_14 > 25:
            reasons.append(f"✓ ADX {indicators.adx_14:.0f} — strong trending conditions")
        else:
            reasons.append(f"⚠ ADX {indicators.adx_14:.0f} — weak trend, be cautious")

        if scores.get("ml_probability", 0) > 0.6:
            reasons.append(f"✓ ML model supports direction (prob={scores['ml_probability']:.2f})")

        return reasons
