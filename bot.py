# ============================================================
# core/bot.py
#
# TradingBot — Main Orchestrator
# ──────────────────────────────────────────────────────────
# Ties all subsystems together into a single event loop:
#
#  1. Fetch multi-timeframe data (DataManager)
#  2. Run market structure analysis (MarketStructureEngine)
#  3. Collect news sentiment (SentimentAnalyser)
#  4. Run ML prediction (ModelTrainer)
#  5. Generate trade signal (StrategyEngine)
#  6. Send alerts (AlertManager)
#  7. Log everything
#
# The bot runs on a configurable schedule (e.g. every H1 candle close).
# It is designed to be stateless — each cycle is independent.
# ============================================================

import asyncio
import signal as _signal
import sys
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from alerts.notifier import AlertManager
from config.settings import CONFIG
from core.market_structure import MarketStructureEngine, Trend
from data.ingestion import DataManager
from ml.model import FeatureEngineer, ModelTrainer, train_model_pipeline
from ml.sentiment import SentimentAnalyser
from strategy.engine import StrategyEngine, TradeSignal


# ────────────────────────────────────────────────
# LOGGING SETUP
# ────────────────────────────────────────────────

def setup_logging():
    logger.remove()
    logger.add(
        sys.stdout,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level:<8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level=CONFIG.log_level,
        colorize=True,
    )
    logger.add(
        "logs/trading_bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",       # Rotate at midnight
        retention="30 days",
        level="DEBUG",
        format="{time} | {level} | {name}:{line} - {message}",
    )


# ────────────────────────────────────────────────
# TRADING BOT
# ────────────────────────────────────────────────

class TradingBot:
    """
    Main bot class. Instantiate once and call bot.start() to begin.

    Architecture:
    ┌─────────────────────────────────────────────┐
    │              TradingBot (Orchestrator)        │
    │                                               │
    │  DataManager ──► StructureEngine ──┐          │
    │                                    ▼          │
    │  SentimentAnalyser ────────► StrategyEngine   │
    │                                    │          │
    │  ML ModelTrainer ──────────────────┘          │
    │                                    ▼          │
    │                             AlertManager      │
    └─────────────────────────────────────────────┘
    """

    def __init__(self):
        self.data_manager = DataManager()
        self.struct_engine = MarketStructureEngine()
        self.strategy = StrategyEngine()
        self.sentiment = SentimentAnalyser()
        self.alert_manager = AlertManager()
        self.feature_engineer = FeatureEngineer()
        self.ml_trainer = ModelTrainer()
        self.scheduler = AsyncIOScheduler(timezone="UTC")

        self._running = False
        self._signal_log: List[Dict] = []

    # ─── LIFECYCLE ────────────────────────────────

    def start(self):
        """Starts the bot and enters the scheduler event loop."""
        setup_logging()
        logger.info("="*60)
        logger.info("  Trading Bot starting...")
        logger.info(f"  Symbols: {CONFIG.market.symbols}")
        logger.info(f"  Primary TF: {CONFIG.market.primary_timeframe}")
        logger.info(f"  Min confidence: {CONFIG.strategy.min_confidence_score}%")
        logger.info("="*60)

        self._running = True

        # Schedule analysis cycle based on primary timeframe
        interval_map = {
            "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
            "H1": 3600, "H4": 14400, "D1": 86400,
        }
        interval_secs = interval_map.get(CONFIG.market.primary_timeframe, 3600)

        # Initial run immediately, then on schedule
        self.scheduler.add_job(
            self._analysis_cycle,
            "interval",
            seconds=interval_secs,
            id="analysis_cycle",
            next_run_time=datetime.now(timezone.utc),
        )

        # ML retraining check (runs every 6 hours)
        self.scheduler.add_job(
            self._retrain_if_needed,
            "interval",
            hours=6,
            id="ml_retrain",
        )

        self.scheduler.start()

        # Handle shutdown gracefully
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            _signal.signal(sig, self._shutdown_handler)

        try:
            asyncio.get_event_loop().run_forever()
        except (KeyboardInterrupt, SystemExit):
            self.stop()

    def stop(self):
        """Gracefully shuts down the bot."""
        logger.info("Trading Bot stopping...")
        self._running = False
        self.scheduler.shutdown()
        self.data_manager.provider.disconnect()
        logger.info("Trading Bot stopped")

    def _shutdown_handler(self, signum, frame):
        logger.info(f"Received signal {signum} — shutting down")
        self.stop()
        sys.exit(0)

    # ─── ANALYSIS CYCLE ───────────────────────────

    async def _analysis_cycle(self):
        """
        Core analysis cycle — runs on each candle close.
        Analyses all configured symbols in parallel.
        """
        logger.info(f"Analysis cycle started — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

        # Fetch sentiment once for all symbols (batch operation)
        try:
            sentiment_scores = self.sentiment.get_scores(CONFIG.market.symbols)
        except Exception as e:
            logger.warning(f"Sentiment fetch failed: {e}")
            sentiment_scores = {s: 0.0 for s in CONFIG.market.symbols}

        # Analyse each symbol
        tasks = [
            self._analyse_symbol(symbol, sentiment_scores.get(symbol, 0.0))
            for symbol in CONFIG.market.symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for symbol, result in zip(CONFIG.market.symbols, results):
            if isinstance(result, Exception):
                logger.error(f"Analysis failed for {symbol}: {result}")
            elif result is not None:
                logger.info(f"Signal: {result}")

    async def _analyse_symbol(
        self,
        symbol: str,
        sentiment_score: float,
    ) -> Optional[TradeSignal]:
        """
        Full analysis pipeline for a single symbol.
        Returns a TradeSignal or None.
        """
        try:
            # 1. Check if we should avoid trading due to news events
            avoid, reason = self.sentiment.should_avoid_trading(symbol)
            if avoid:
                logger.warning(f"Trading paused for {symbol}: {reason}")
                return None

            # 2. Fetch multi-timeframe data
            mtf_data = self.data_manager.get_mtf_data(symbol)

            primary_tf = CONFIG.market.primary_timeframe
            higher_tf  = CONFIG.market.higher_timeframe

            df_primary = mtf_data.get(primary_tf)
            df_htf     = mtf_data.get(higher_tf)

            if df_primary is None or df_primary.empty:
                logger.warning(f"No primary data for {symbol}")
                return None

            # 3. Run market structure analysis
            primary_analysis = self.struct_engine.analyse(
                df_primary, symbol, primary_tf
            )

            if df_htf is not None and not df_htf.empty:
                htf_analysis = self.struct_engine.analyse(
                    df_htf, symbol, higher_tf
                )
            else:
                htf_analysis = primary_analysis

            struct_summary = self.struct_engine.summarise(primary_analysis)
            logger.info(
                f"{symbol} {primary_tf}: "
                f"Trend={struct_summary['trend']} | "
                f"BOS={struct_summary['bos_bullish_count']}↑ {struct_summary['bos_bearish_count']}↓ | "
                f"FVG={struct_summary['unfilled_bull_fvg_count']}↑ {struct_summary['unfilled_bear_fvg_count']}↓"
            )

            # 4. Get ML probability (if model is trained)
            ml_prob = 0.5
            try:
                features = self.feature_engineer.build_features(df_primary)
                if not features.empty:
                    ml_prob = self.ml_trainer.predict_probability(features)
            except Exception as e:
                logger.debug(f"ML prediction skipped for {symbol}: {e}")

            # 5. Generate trade signal
            signal = self.strategy.generate_signal(
                symbol=symbol,
                primary_analysis=primary_analysis,
                htf_analysis=htf_analysis,
                df_primary=df_primary,
                ml_probability=ml_prob,
                sentiment_score=sentiment_score,
            )

            if signal is None:
                logger.debug(f"{symbol}: No valid signal this cycle")
                return None

            # 6. Log and alert
            logger.info(f"HIGH-CONFIDENCE SIGNAL: {signal}")
            self._signal_log.append(signal.to_dict())
            self.alert_manager.process_signal(signal)

            return signal

        except Exception as e:
            logger.error(f"Symbol analysis failed ({symbol}): {e}")
            logger.debug(traceback.format_exc())
            return None

    # ─── ML RETRAINING ────────────────────────────

    async def _retrain_if_needed(self):
        """Retrains the ML model if it is stale."""
        if not self.ml_trainer.needs_retraining():
            logger.debug("ML model is up to date — no retraining needed")
            return

        logger.info("Starting ML model retraining...")
        try:
            # Collect training data for all symbols
            all_dfs = []
            for symbol in CONFIG.market.symbols:
                df = self.data_manager.provider.fetch_ohlcv(
                    symbol,
                    CONFIG.market.primary_timeframe,
                    limit=2000,
                )
                if not df.empty:
                    all_dfs.append(df)

            if not all_dfs:
                logger.warning("No training data available")
                return

            combined_df = pd.concat(all_dfs)
            combined_df.sort_index(inplace=True)

            trainer, metrics = train_model_pipeline(combined_df)
            self.ml_trainer = trainer

            logger.info(
                f"ML retraining complete: "
                f"AUC={metrics['mean_auc']:.3f} "
                f"Acc={metrics['mean_accuracy']:.3f}"
            )
        except Exception as e:
            logger.error(f"ML retraining failed: {e}")

    # ─── MANUAL METHODS ───────────────────────────

    def run_backtest(self, symbol: str, timeframe: str = "H1"):
        """
        Runs a full backtest. Call directly for manual testing.
        Usage: bot.run_backtest("BTCUSDT", "H1")
        """
        from backtest.engine import BacktestEngine
        import pandas as pd

        engine = BacktestEngine()
        df = self.data_manager.provider.fetch_ohlcv(
            symbol, timeframe, limit=2000
        )
        df_htf = self.data_manager.provider.fetch_ohlcv(
            symbol, CONFIG.market.higher_timeframe, limit=500
        )

        return engine.run(df, symbol=symbol, timeframe=timeframe, df_htf=df_htf)

    def get_signal_history(self) -> List[Dict]:
        """Returns all signals generated in this session."""
        return self._signal_log.copy()


# ────────────────────────────────────────────────
# ENTRY POINT
# ────────────────────────────────────────────────

import pandas as pd  # Required for _retrain_if_needed

if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
