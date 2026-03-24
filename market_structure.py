# ============================================================
# core/market_structure.py
#
# Market Structure Engine
# ──────────────────────────────────────────────────────────
# Implements rigorous algorithmic definitions for:
#
#  1. Swing High / Swing Low detection
#  2. Break of Structure (BOS)
#  3. Change of Character (CHoCH)
#  4. Fair Value Gaps (FVG)
#  5. Liquidity Zones (Equal Highs/Lows, Stop Hunt zones)
#  6. Trend Bias (Higher-Timeframe context)
#
# All functions are pure (no side effects) and accept
# a standard OHLCV DataFrame returned by DataManager.
# ============================================================

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import CONFIG


# ────────────────────────────────────────────────
# ENUMS & DATA CLASSES
# ────────────────────────────────────────────────

class Trend(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGING = "RANGING"


class SignalType(Enum):
    BUY  = "BUY"
    SELL = "SELL"
    NONE = "NONE"


@dataclass
class SwingPoint:
    """A confirmed swing high or swing low."""
    index: int              # Bar index in the DataFrame
    timestamp: pd.Timestamp
    price: float
    is_high: bool           # True = swing high, False = swing low


@dataclass
class BOS:
    """Break of Structure event."""
    bar_index: int
    timestamp: pd.Timestamp
    direction: Trend        # BULLISH = broke above prev high, BEARISH = broke below prev low
    broken_level: float     # The structural high/low that was broken
    close_price: float      # Close price at the bar of the break
    confirmed: bool = True


@dataclass
class CHoCH:
    """Change of Character event — first sign of trend reversal."""
    bar_index: int
    timestamp: pd.Timestamp
    direction: Trend        # New direction AFTER the character change
    broken_level: float     # The minor high/low that was broken against the trend
    prior_trend: Trend


@dataclass
class FairValueGap:
    """
    Fair Value Gap (FVG) — imbalance created by a 3-candle sequence
    where candle[i-1].high < candle[i+1].low (bullish)
    or  candle[i-1].low  > candle[i+1].high (bearish)
    """
    bar_index: int          # Index of the middle (impulse) candle
    timestamp: pd.Timestamp
    gap_top: float
    gap_bottom: float
    direction: Trend        # BULLISH = demand FVG, BEARISH = supply FVG
    size_pips: float
    filled: bool = False    # True once price revisits the gap


@dataclass
class LiquidityZone:
    """Zone where stop orders are likely clustered."""
    price_level: float
    zone_type: str          # "equal_highs" | "equal_lows" | "previous_high" | "previous_low"
    bar_index: int
    swept: bool = False     # True once price pierces and rejects the level


@dataclass
class StructureAnalysis:
    """Complete market structure snapshot for one symbol/timeframe."""
    symbol: str
    timeframe: str
    timestamp: pd.Timestamp
    trend: Trend
    swing_highs: List[SwingPoint]
    swing_lows: List[SwingPoint]
    bos_events: List[BOS]
    choch_events: List[CHoCH]
    fvgs: List[FairValueGap]
    liquidity_zones: List[LiquidityZone]
    current_price: float
    last_bos: Optional[BOS] = None
    last_choch: Optional[CHoCH] = None


# ────────────────────────────────────────────────
# CORE ENGINE CLASS
# ────────────────────────────────────────────────

class MarketStructureEngine:
    """
    Stateless engine — call analyse(df) to get a StructureAnalysis.
    All methods are deterministic given the same input DataFrame.
    """

    def __init__(self):
        self.cfg = CONFIG.structure

    # ─── PUBLIC INTERFACE ─────────────────────────────────

    def analyse(self, df: pd.DataFrame, symbol: str = "", timeframe: str = "") -> StructureAnalysis:
        """
        Master analysis function.
        1. Detect swing points
        2. Determine trend direction
        3. Detect BOS events
        4. Detect CHoCH events
        5. Detect FVGs
        6. Map liquidity zones
        """
        if len(df) < self.cfg.swing_lookback * 2 + 10:
            raise ValueError(f"Insufficient data: need >{self.cfg.swing_lookback * 2 + 10} bars")

        swings = self._detect_swing_points(df)
        swing_highs = [s for s in swings if s.is_high]
        swing_lows  = [s for s in swings if not s.is_high]

        trend = self._determine_trend(swing_highs, swing_lows)
        bos_events = self._detect_bos(df, swing_highs, swing_lows, trend)
        choch_events = self._detect_choch(df, swing_highs, swing_lows, trend)
        fvgs = self._detect_fvg(df)
        liq_zones = self._detect_liquidity_zones(df, swing_highs, swing_lows)

        # Mark FVGs as filled if price has re-entered the gap
        current_price = df["close"].iloc[-1]
        self._update_fvg_fill_status(fvgs, current_price)

        return StructureAnalysis(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=df.index[-1],
            trend=trend,
            swing_highs=swing_highs[-10:],   # Keep last 10 for readability
            swing_lows=swing_lows[-10:],
            bos_events=bos_events[-5:],
            choch_events=choch_events[-3:],
            fvgs=[f for f in fvgs if not f.filled],  # Only unfilled FVGs
            liquidity_zones=[z for z in liq_zones if not z.swept],
            current_price=current_price,
            last_bos=bos_events[-1] if bos_events else None,
            last_choch=choch_events[-1] if choch_events else None,
        )

    # ─── 1. SWING POINT DETECTION ─────────────────────────

    def _detect_swing_points(self, df: pd.DataFrame) -> List[SwingPoint]:
        """
        Swing High: high[i] is the highest among the N bars before AND after bar i.
        Swing Low:  low[i]  is the lowest  among the N bars before AND after bar i.

        This is equivalent to:
          swing_high[i] = (high[i] == max(high[i-N : i+N+1]))
          swing_low[i]  = (low[i]  == min(low[i-N : i+N+1]))

        N = swing_lookback (default = 5)
        """
        N = self.cfg.swing_lookback
        swings: List[SwingPoint] = []

        highs = df["high"].values
        lows  = df["low"].values

        for i in range(N, len(df) - N):
            window_highs = highs[i - N : i + N + 1]
            window_lows  = lows[i - N : i + N + 1]

            if highs[i] == np.max(window_highs):
                swings.append(SwingPoint(
                    index=i,
                    timestamp=df.index[i],
                    price=highs[i],
                    is_high=True,
                ))

            if lows[i] == np.min(window_lows):
                swings.append(SwingPoint(
                    index=i,
                    timestamp=df.index[i],
                    price=lows[i],
                    is_high=False,
                ))

        return sorted(swings, key=lambda s: s.index)

    # ─── 2. TREND DETERMINATION ───────────────────────────

    def _determine_trend(
        self,
        swing_highs: List[SwingPoint],
        swing_lows: List[SwingPoint],
    ) -> Trend:
        """
        Trend is determined by the sequence of swing highs and lows:

        BULLISH:  Higher Highs (HH) + Higher Lows (HL)
          → Each swing high is above the previous swing high
          → Each swing low  is above the previous swing low

        BEARISH:  Lower Highs (LH) + Lower Lows (LL)
          → Each swing high is below the previous swing high
          → Each swing low  is below the previous swing low

        RANGING: Mixed structure (no consistent HH/HL or LH/LL pattern)

        We require at least 3 consecutive swing points to confirm a trend.
        """
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return Trend.RANGING

        # Check the last 3 swing highs
        recent_highs = [s.price for s in swing_highs[-3:]]
        recent_lows  = [s.price for s in swing_lows[-3:]]

        # Count how many are HH/HL vs LH/LL
        hh_count = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i] > recent_highs[i-1])
        hl_count = sum(1 for i in range(1, len(recent_lows))  if recent_lows[i]  > recent_lows[i-1])
        lh_count = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i] < recent_highs[i-1])
        ll_count = sum(1 for i in range(1, len(recent_lows))  if recent_lows[i]  < recent_lows[i-1])

        bull_score = hh_count + hl_count
        bear_score = lh_count + ll_count

        if bull_score > bear_score and bull_score >= 2:
            return Trend.BULLISH
        elif bear_score > bull_score and bear_score >= 2:
            return Trend.BEARISH
        else:
            return Trend.RANGING

    # ─── 3. BREAK OF STRUCTURE (BOS) ──────────────────────

    def _detect_bos(
        self,
        df: pd.DataFrame,
        swing_highs: List[SwingPoint],
        swing_lows: List[SwingPoint],
        trend: Trend,
    ) -> List[BOS]:
        """
        BOS DEFINITION (in context of an established trend):

        Bullish BOS: In a BULLISH trend, price closes ABOVE the most recent
                     confirmed swing HIGH. This confirms trend continuation.

                     Condition: close[i] > swing_high[-1].price
                     AND bar i occurs AFTER the swing_high was confirmed

        Bearish BOS: In a BEARISH trend, price closes BELOW the most recent
                     confirmed swing LOW. This confirms trend continuation.

                     Condition: close[i] < swing_low[-1].price

        A BOS only counts once per structural level (no re-detection
        of the same level).
        """
        bos_events: List[BOS] = []
        closes = df["close"].values
        detected_levels = set()  # Prevent duplicate detections

        def _add_bos(i, direction, broken_level):
            level_key = round(broken_level, 5)
            if level_key in detected_levels:
                return
            detected_levels.add(level_key)
            bos_events.append(BOS(
                bar_index=i,
                timestamp=df.index[i],
                direction=direction,
                broken_level=broken_level,
                close_price=closes[i],
            ))

        # Bullish BOS: close breaks above a prior swing high
        for sh in swing_highs:
            for i in range(sh.index + self.cfg.bos_confirmation_bars + 1, len(df)):
                if closes[i] > sh.price:
                    _add_bos(i, Trend.BULLISH, sh.price)
                    break   # One BOS per swing level

        # Bearish BOS: close breaks below a prior swing low
        for sl in swing_lows:
            for i in range(sl.index + self.cfg.bos_confirmation_bars + 1, len(df)):
                if closes[i] < sl.price:
                    _add_bos(i, Trend.BEARISH, sl.price)
                    break

        return sorted(bos_events, key=lambda b: b.bar_index)

    # ─── 4. CHANGE OF CHARACTER (CHoCH) ───────────────────

    def _detect_choch(
        self,
        df: pd.DataFrame,
        swing_highs: List[SwingPoint],
        swing_lows: List[SwingPoint],
        trend: Trend,
    ) -> List[CHoCH]:
        """
        CHoCH DEFINITION:
        Unlike BOS (which confirms trend continuation), CHoCH signals the
        FIRST sign of a possible trend reversal.

        Bullish CHoCH (potential reversal from bearish to bullish):
          - We are in a BEARISH trend (LH + LL sequence)
          - Price closes ABOVE the MOST RECENT minor swing HIGH
            (a high that formed during the bearish pullback)
          - This is the first time price has broken structure to the upside

        Bearish CHoCH (potential reversal from bullish to bearish):
          - We are in a BULLISH trend (HH + HL sequence)
          - Price closes BELOW the MOST RECENT minor swing LOW
            (a low that formed during a bullish pullback)
          - This is the first time price has broken structure to the downside

        KEY DISTINCTION from BOS:
          BOS  = breaks the MAJOR structural level (confirms trend direction)
          CHoCH = breaks the MINOR structural level AGAINST the trend
                  (first sign of reversal)
        """
        choch_events: List[CHoCH] = []
        closes = df["close"].values

        if trend == Trend.BEARISH and len(swing_highs) >= 2:
            # Look for a break ABOVE the most recent swing high in a downtrend
            # (minor high = the pullback high within the downtrend)
            for sh in reversed(swing_highs[-5:]):    # Check last 5 swing highs
                for i in range(sh.index + 1, min(sh.index + self.cfg.choch_lookback, len(df))):
                    if closes[i] > sh.price:
                        choch_events.append(CHoCH(
                            bar_index=i,
                            timestamp=df.index[i],
                            direction=Trend.BULLISH,
                            broken_level=sh.price,
                            prior_trend=Trend.BEARISH,
                        ))
                        break

        elif trend == Trend.BULLISH and len(swing_lows) >= 2:
            # Look for a break BELOW the most recent swing low in an uptrend
            for sl in reversed(swing_lows[-5:]):
                for i in range(sl.index + 1, min(sl.index + self.cfg.choch_lookback, len(df))):
                    if closes[i] < sl.price:
                        choch_events.append(CHoCH(
                            bar_index=i,
                            timestamp=df.index[i],
                            direction=Trend.BEARISH,
                            broken_level=sl.price,
                            prior_trend=Trend.BULLISH,
                        ))
                        break

        return sorted(choch_events, key=lambda c: c.bar_index)

    # ─── 5. FAIR VALUE GAPS (FVG) ─────────────────────────

    def _detect_fvg(self, df: pd.DataFrame) -> List[FairValueGap]:
        """
        FVG DEFINITION:
        A Fair Value Gap occurs when a STRONG impulse candle (candle[i])
        creates a price gap between:
          - candle[i-1].high  (top of pre-impulse candle)
          - candle[i+1].low   (bottom of post-impulse candle)

        BULLISH FVG (demand imbalance):
          Condition: candle[i+1].low > candle[i-1].high
          Gap range: [candle[i-1].high, candle[i+1].low]
          Interpretation: Price moved up so fast it left an "unfilled" area.
                          Bulls will likely defend this zone on a pullback.

        BEARISH FVG (supply imbalance):
          Condition: candle[i+1].high < candle[i-1].low
          Gap range: [candle[i+1].high, candle[i-1].low]
          Interpretation: Price moved down so fast it left an unfilled area.
                          Bears will likely defend this zone on a pullback.

        We also enforce a minimum gap size to filter noise.
        """
        fvgs: List[FairValueGap] = []
        pip_size = self._estimate_pip_size(df)

        for i in range(1, len(df) - 1):
            prev_high = df["high"].iloc[i - 1]
            prev_low  = df["low"].iloc[i - 1]
            next_high = df["high"].iloc[i + 1]
            next_low  = df["low"].iloc[i + 1]

            # ── Bullish FVG ──────────────────────────────
            if next_low > prev_high:
                gap_size_pips = (next_low - prev_high) / pip_size
                if gap_size_pips >= self.cfg.fvg_min_size_pips:
                    fvgs.append(FairValueGap(
                        bar_index=i,
                        timestamp=df.index[i],
                        gap_top=next_low,
                        gap_bottom=prev_high,
                        direction=Trend.BULLISH,
                        size_pips=gap_size_pips,
                    ))

            # ── Bearish FVG ──────────────────────────────
            elif next_high < prev_low:
                gap_size_pips = (prev_low - next_high) / pip_size
                if gap_size_pips >= self.cfg.fvg_min_size_pips:
                    fvgs.append(FairValueGap(
                        bar_index=i,
                        timestamp=df.index[i],
                        gap_top=prev_low,
                        gap_bottom=next_high,
                        direction=Trend.BEARISH,
                        size_pips=gap_size_pips,
                    ))

        return fvgs

    def _update_fvg_fill_status(self, fvgs: List[FairValueGap], current_price: float):
        """Mark FVGs as filled if current price is inside the gap range."""
        for fvg in fvgs:
            if fvg.gap_bottom <= current_price <= fvg.gap_top:
                fvg.filled = True

    # ─── 6. LIQUIDITY ZONES ───────────────────────────────

    def _detect_liquidity_zones(
        self,
        df: pd.DataFrame,
        swing_highs: List[SwingPoint],
        swing_lows: List[SwingPoint],
    ) -> List[LiquidityZone]:
        """
        Liquidity zones are price levels where stop-loss orders are clustered.
        Market makers and institutional traders often target these levels.

        Types detected:
        1. Equal Highs (EQH): Two or more swing highs within N pips of each other
           → Buy-stops are stacked above these levels
        2. Equal Lows  (EQL): Two or more swing lows within N pips of each other
           → Sell-stops are stacked below these levels
        3. Prior swing highs/lows: Major structural levels from the lookback period

        A zone is considered "swept" if price pierces it and then closes back
        inside (typical stop-hunt / liquidity grab behaviour).
        """
        zones: List[LiquidityZone] = []
        threshold = self.cfg.liquidity_zone_threshold
        closes = df["close"].values

        # Equal Highs
        for i, sh1 in enumerate(swing_highs[:-1]):
            for sh2 in swing_highs[i + 1:]:
                if abs(sh1.price - sh2.price) / sh1.price < threshold:
                    level = (sh1.price + sh2.price) / 2
                    zones.append(LiquidityZone(
                        price_level=level,
                        zone_type="equal_highs",
                        bar_index=sh2.index,
                    ))

        # Equal Lows
        for i, sl1 in enumerate(swing_lows[:-1]):
            for sl2 in swing_lows[i + 1:]:
                if abs(sl1.price - sl2.price) / sl1.price < threshold:
                    level = (sl1.price + sl2.price) / 2
                    zones.append(LiquidityZone(
                        price_level=level,
                        zone_type="equal_lows",
                        bar_index=sl2.index,
                    ))

        # Prior swing highs/lows as structural liquidity
        for sh in swing_highs[-5:]:
            zones.append(LiquidityZone(
                price_level=sh.price,
                zone_type="previous_high",
                bar_index=sh.index,
            ))
        for sl in swing_lows[-5:]:
            zones.append(LiquidityZone(
                price_level=sl.price,
                zone_type="previous_low",
                bar_index=sl.index,
            ))

        # Mark zones swept by recent price action
        recent_closes = closes[-20:]
        for zone in zones:
            for c in recent_closes:
                dist = abs(c - zone.price_level) / zone.price_level
                if dist < threshold * 0.5:
                    zone.swept = True

        return zones

    # ─── UTILITIES ────────────────────────────────────────

    @staticmethod
    def _estimate_pip_size(df: pd.DataFrame) -> float:
        """
        Estimate pip size from the data.
        Forex 5-digit: ~0.00001
        Forex 4-digit: ~0.0001
        Crypto (BTC):  ~1.0
        """
        avg_price = df["close"].mean()
        if avg_price > 100:
            return 1.0       # Crypto / indices
        elif avg_price > 1:
            return 0.0001    # Standard forex (JPY pairs etc.)
        else:
            return 0.00001   # 5-digit forex

    def get_nearest_fvg(
        self,
        fvgs: List[FairValueGap],
        current_price: float,
        direction: Trend,
    ) -> Optional[FairValueGap]:
        """
        Returns the closest unfilled FVG in the specified direction.
        Used by the strategy engine to find entry confluences.
        """
        relevant = [
            f for f in fvgs
            if not f.filled and f.direction == direction
        ]
        if not relevant:
            return None
        return min(relevant, key=lambda f: abs(
            ((f.gap_top + f.gap_bottom) / 2) - current_price
        ))

    def summarise(self, analysis: StructureAnalysis) -> dict:
        """
        Returns a flat dictionary suitable for ML feature extraction
        and logging.
        """
        return {
            "trend": analysis.trend.value,
            "swing_high_count": len(analysis.swing_highs),
            "swing_low_count": len(analysis.swing_lows),
            "bos_bullish_count": sum(1 for b in analysis.bos_events if b.direction == Trend.BULLISH),
            "bos_bearish_count": sum(1 for b in analysis.bos_events if b.direction == Trend.BEARISH),
            "choch_count": len(analysis.choch_events),
            "unfilled_bull_fvg_count": sum(1 for f in analysis.fvgs if f.direction == Trend.BULLISH),
            "unfilled_bear_fvg_count": sum(1 for f in analysis.fvgs if f.direction == Trend.BEARISH),
            "liquidity_zone_count": len(analysis.liquidity_zones),
            "last_bos_direction": analysis.last_bos.direction.value if analysis.last_bos else None,
            "last_choch_direction": analysis.last_choch.direction.value if analysis.last_choch else None,
            "current_price": analysis.current_price,
        }
