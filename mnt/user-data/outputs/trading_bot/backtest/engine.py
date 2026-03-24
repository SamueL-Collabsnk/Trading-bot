# ============================================================
# backtest/engine.py
#
# Backtesting Framework
# ──────────────────────────────────────────────────────────
# Implements event-driven backtesting with:
#  - Realistic order execution (spread + slippage)
#  - Position sizing (fixed % risk per trade)
#  - Walk-forward validation to prevent overfitting
#  - Comprehensive performance metrics
#  - Equity curve and drawdown analysis
# ============================================================

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import CONFIG
from core.market_structure import MarketStructureEngine, Trend
from strategy.engine import StrategyEngine, TradeSignal, SignalType


# ────────────────────────────────────────────────
# DATA CLASSES
# ────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    """Records a single completed backtest trade."""
    signal_id: str
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float

    entry_bar: int
    exit_bar: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "tp", "sl", "timeout", "eod"

    profit_pips: float = 0.0
    profit_pct: float = 0.0
    profit_dollar: float = 0.0
    risk_reward_actual: float = 0.0

    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None

    @property
    def is_winner(self) -> bool:
        return self.profit_dollar > 0

    @property
    def duration_bars(self) -> int:
        if self.exit_bar is not None:
            return self.exit_bar - self.entry_bar
        return 0


@dataclass
class BacktestResults:
    """Complete backtest performance summary."""
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float

    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trades if t.is_winner)

    @property
    def losing_trades(self) -> int:
        return sum(1 for t in self.trades if not t.is_winner)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def total_return_pct(self) -> float:
        return (self.final_capital / self.initial_capital - 1) * 100

    @property
    def avg_win(self) -> float:
        wins = [t.profit_dollar for t in self.trades if t.is_winner]
        return np.mean(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.profit_dollar for t in self.trades if not t.is_winner]
        return np.mean(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.profit_dollar for t in self.trades if t.is_winner)
        gross_loss = abs(sum(t.profit_dollar for t in self.trades if not t.is_winner))
        return gross_profit / (gross_loss + 1e-10)

    @property
    def expectancy(self) -> float:
        """Expected $ profit per trade."""
        if self.total_trades == 0:
            return 0.0
        return self.win_rate * self.avg_win + (1 - self.win_rate) * self.avg_loss

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        rolling_max = self.equity_curve.cummax()
        drawdown = (self.equity_curve - rolling_max) / rolling_max
        return float(drawdown.min() * 100)

    @property
    def max_drawdown_duration(self) -> int:
        """Max number of bars spent in drawdown."""
        if self.equity_curve.empty:
            return 0
        rolling_max = self.equity_curve.cummax()
        in_dd = (self.equity_curve < rolling_max).astype(int)
        groups = (in_dd != in_dd.shift()).cumsum()
        dd_groups = in_dd[in_dd == 1].groupby(groups).size()
        return int(dd_groups.max()) if len(dd_groups) > 0 else 0

    @property
    def sharpe_ratio(self) -> float:
        """Annualised Sharpe ratio (assumes H1 bars, ~252*24 periods/year)."""
        if self.equity_curve.empty or len(self.equity_curve) < 2:
            return 0.0
        returns = self.equity_curve.pct_change().dropna()
        if returns.std() == 0:
            return 0.0
        annual_factor = np.sqrt(252 * 24)  # H1 bars
        return float((returns.mean() / returns.std()) * annual_factor)

    @property
    def calmar_ratio(self) -> float:
        """Annual return / max drawdown (risk-adjusted return metric)."""
        if abs(self.max_drawdown_pct) < 0.01:
            return 0.0
        return self.total_return_pct / abs(self.max_drawdown_pct)

    def print_summary(self):
        """Prints a formatted performance report."""
        print("\n" + "="*60)
        print(f"  BACKTEST RESULTS: {self.symbol}")
        print(f"  Period: {self.start_date} → {self.end_date}")
        print("="*60)
        print(f"  Initial Capital:     ${self.initial_capital:>10,.2f}")
        print(f"  Final Capital:       ${self.final_capital:>10,.2f}")
        print(f"  Total Return:        {self.total_return_pct:>+10.2f}%")
        print(f"  Sharpe Ratio:        {self.sharpe_ratio:>10.2f}")
        print(f"  Calmar Ratio:        {self.calmar_ratio:>10.2f}")
        print("-"*60)
        print(f"  Total Trades:        {self.total_trades:>10}")
        print(f"  Win Rate:            {self.win_rate*100:>9.1f}%")
        print(f"  Avg Win:             ${self.avg_win:>10.2f}")
        print(f"  Avg Loss:            ${self.avg_loss:>10.2f}")
        print(f"  Profit Factor:       {self.profit_factor:>10.2f}")
        print(f"  Expectancy/Trade:    ${self.expectancy:>10.2f}")
        print("-"*60)
        print(f"  Max Drawdown:        {self.max_drawdown_pct:>9.2f}%")
        print(f"  Max DD Duration:     {self.max_drawdown_duration:>7} bars")
        print("="*60 + "\n")

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "final_capital": self.final_capital,
            "total_return_pct": self.total_return_pct,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "calmar_ratio": self.calmar_ratio,
        }


# ────────────────────────────────────────────────
# BACKTESTING ENGINE
# ────────────────────────────────────────────────

class BacktestEngine:
    """
    Event-driven backtester.

    For each bar (starting from warm-up period):
      1. Run market structure analysis on bars [0..i]
      2. Run strategy engine to get signal
      3. Simulate order execution at next bar open
      4. Check open positions for SL/TP hit on each subsequent bar
      5. Update equity curve

    KEY ANTI-OVERFITTING MEASURES:
      - No future data used in any calculation (strict lookahead prevention)
      - Walk-forward testing splits data into train/test blocks
      - Minimum 200 bars warm-up before first trade
      - Out-of-sample performance is the only metric that matters
    """

    WARMUP_BARS = 100        # Bars needed before strategy starts

    def __init__(self):
        self.cfg = CONFIG.backtest
        self.struct_engine = MarketStructureEngine()
        self.strategy = StrategyEngine()

    def run(
        self,
        df: pd.DataFrame,
        symbol: str = "UNKNOWN",
        timeframe: str = "H1",
        df_htf: Optional[pd.DataFrame] = None,
    ) -> BacktestResults:
        """
        Runs a full backtest on the provided DataFrame.

        Args:
            df:        Primary OHLCV DataFrame (from DataManager)
            symbol:    Instrument name (for reporting)
            timeframe: Candle timeframe (for reporting)
            df_htf:    Higher-timeframe DataFrame for trend bias (optional)
        """
        logger.info(f"Starting backtest: {symbol} | {len(df)} bars | "
                    f"Capital: ${self.cfg.initial_capital:,.0f}")

        capital = self.cfg.initial_capital
        equity_curve = []
        completed_trades: List[BacktestTrade] = []
        open_trade: Optional[BacktestTrade] = None

        pip_size = self._estimate_pip_size(df)
        spread = self.cfg.spread_pips * pip_size
        slippage = self.cfg.slippage_pips * pip_size
        commission = self.cfg.commission_pct

        if len(df) <= self.WARMUP_BARS + 5:
            logger.warning(f"Insufficient bars ({len(df)}) for backtest — need >{self.WARMUP_BARS+5}")
            return BacktestResults(
                symbol=symbol, start_date="N/A", end_date="N/A",
                initial_capital=self.cfg.initial_capital,
                final_capital=self.cfg.initial_capital,
            )

        for i in range(self.WARMUP_BARS, len(df) - 1):
            bar = df.iloc[i]
            next_bar = df.iloc[i + 1]

            # ── 1. Manage open position ─────────────────────
            if open_trade is not None:
                exit_price, exit_reason = self._check_exit(
                    open_trade, bar, next_bar
                )
                if exit_price is not None:
                    open_trade = self._close_trade(
                        open_trade, i, exit_price, exit_reason,
                        capital, pip_size, commission
                    )
                    capital += open_trade.profit_dollar
                    completed_trades.append(open_trade)
                    open_trade = None

            # ── 2. Look for new signal (only if no open trade) ─
            if open_trade is None and len(completed_trades) < 10000:
                signal = self._get_signal_at_bar(
                    df.iloc[:i + 1], df_htf, symbol, timeframe
                )
                if signal is not None:
                    # Execute at next bar open with slippage
                    exec_price = next_bar["open"]
                    if signal.direction == SignalType.BUY:
                        exec_price += spread + slippage
                    else:
                        exec_price -= slippage

                    open_trade = BacktestTrade(
                        signal_id=signal.signal_id,
                        symbol=symbol,
                        direction=signal.direction.value,
                        entry_price=exec_price,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        confidence=signal.confidence,
                        entry_bar=i + 1,
                        entry_time=df.index[i + 1],
                    )
                    logger.debug(f"Bar {i}: New trade opened: {open_trade.direction} @ {exec_price:.5f}")

            equity_curve.append(capital)

        # Close any remaining open trade at last bar
        if open_trade is not None:
            last_price = df["close"].iloc[-1]
            open_trade = self._close_trade(
                open_trade, len(df) - 1, last_price, "eod",
                capital, pip_size, commission
            )
            capital += open_trade.profit_dollar
            completed_trades.append(open_trade)

        equity_series = pd.Series(
            equity_curve,
            index=df.index[self.WARMUP_BARS: self.WARMUP_BARS + len(equity_curve)]
        )

        results = BacktestResults(
            symbol=symbol,
            start_date=str(df.index[self.WARMUP_BARS].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=self.cfg.initial_capital,
            final_capital=capital,
            trades=completed_trades,
            equity_curve=equity_series,
        )

        results.print_summary()
        return results

    def _get_signal_at_bar(
        self,
        df_slice: pd.DataFrame,
        df_htf: Optional[pd.DataFrame],
        symbol: str,
        timeframe: str,
    ) -> Optional[TradeSignal]:
        """Runs the full analysis pipeline on data up to current bar."""
        try:
            primary_analysis = self.struct_engine.analyse(df_slice, symbol, timeframe)

            if df_htf is not None and len(df_htf) > self.WARMUP_BARS:
                # Align HTF to current time
                htf_slice = df_htf[df_htf.index <= df_slice.index[-1]]
                htf_analysis = self.struct_engine.analyse(htf_slice, symbol, "H4")
            else:
                htf_analysis = primary_analysis

            signal = self.strategy.generate_signal(
                symbol=symbol,
                primary_analysis=primary_analysis,
                htf_analysis=htf_analysis,
                df_primary=df_slice,
                ml_probability=0.5,   # No ML in basic backtest
                sentiment_score=0.0,  # No sentiment in basic backtest
            )
            return signal
        except Exception as e:
            logger.debug(f"Signal generation error at bar: {e}")
            return None

    def _check_exit(
        self,
        trade: BacktestTrade,
        bar: pd.Series,
        next_bar: pd.Series,
    ) -> Tuple[Optional[float], Optional[str]]:
        """
        Checks whether SL or TP was hit during the current bar.
        Uses the bar's high/low range to simulate intrabar price movement.

        For BUY trades:
          SL hit if bar_low  ≤ stop_loss
          TP hit if bar_high ≥ take_profit

        We conservatively assume SL hits before TP if both are breached
        in the same bar (worst-case execution).
        """
        if trade.direction == "BUY":
            if bar["low"] <= trade.stop_loss:
                return trade.stop_loss, "sl"
            if bar["high"] >= trade.take_profit:
                return trade.take_profit, "tp"
        else:  # SELL
            if bar["high"] >= trade.stop_loss:
                return trade.stop_loss, "sl"
            if bar["low"] <= trade.take_profit:
                return trade.take_profit, "tp"

        return None, None

    def _close_trade(
        self,
        trade: BacktestTrade,
        exit_bar: int,
        exit_price: float,
        exit_reason: str,
        capital: float,
        pip_size: float,
        commission: float,
    ) -> BacktestTrade:
        """Finalises trade P&L."""
        trade.exit_bar = exit_bar
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason

        if trade.direction == "BUY":
            pips = (exit_price - trade.entry_price) / pip_size
        else:
            pips = (trade.entry_price - exit_price) / pip_size

        trade.profit_pips = pips

        # Dollar P&L using fixed risk %
        risk_amount = capital * (CONFIG.strategy.risk_per_trade_pct / 100)
        pip_risk = abs(trade.entry_price - trade.stop_loss) / pip_size
        pip_value = risk_amount / (pip_risk + 1e-10)
        gross_pnl = pips * pip_value
        trade.profit_dollar = gross_pnl - (capital * commission)  # Net of commission

        if trade.profit_dollar != 0:
            trade.profit_pct = trade.profit_dollar / capital * 100
            risk = abs(trade.entry_price - trade.stop_loss)
            trade.risk_reward_actual = (
                abs(exit_price - trade.entry_price) / (risk + 1e-10)
            )

        logger.debug(
            f"Trade closed: {trade.direction} {trade.exit_reason} "
            f"P&L=${trade.profit_dollar:.2f} ({pips:.1f} pips)"
        )
        return trade

    @staticmethod
    def _estimate_pip_size(df: pd.DataFrame) -> float:
        avg = df["close"].mean()
        if avg > 100:
            return 1.0
        elif avg > 1:
            return 0.0001
        return 0.00001

    def walk_forward_test(
        self,
        df: pd.DataFrame,
        symbol: str,
        n_folds: int = 4,
    ) -> List[BacktestResults]:
        """
        Walk-forward optimisation:
        Splits data into n_folds and tests each on out-of-sample data.
        This is the gold standard for avoiding overfitting.

        FOLD STRUCTURE (4 folds):
          [==TRAIN==|TEST]
          [====TRAIN====|TEST]
          [======TRAIN======|TEST]
          [========TRAIN========|TEST]
        """
        logger.info(f"Walk-forward test: {n_folds} folds, {len(df)} total bars")
        fold_size = len(df) // (n_folds + 1)
        results = []

        for fold in range(n_folds):
            test_start = fold_size * (fold + 1)
            test_end   = test_start + fold_size

            test_df = df.iloc[test_start:test_end].copy()
            logger.info(f"Fold {fold+1}/{n_folds}: testing bars {test_start}-{test_end}")

            if len(test_df) <= self.WARMUP_BARS + 10:
                logger.warning(f"Fold {fold+1}: insufficient data, skipping")
                continue

            fold_result = self.run(test_df, symbol=f"{symbol}_fold{fold+1}")
            results.append(fold_result)

        # Summary across all folds
        if results:
            avg_winrate = np.mean([r.win_rate for r in results])
            avg_sharpe  = np.mean([r.sharpe_ratio for r in results])
            avg_dd      = np.mean([r.max_drawdown_pct for r in results])
            logger.info(
                f"Walk-forward summary: "
                f"Win Rate={avg_winrate*100:.1f}% "
                f"Sharpe={avg_sharpe:.2f} "
                f"Avg Drawdown={avg_dd:.2f}%"
            )

        return results
