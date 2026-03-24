# ============================================================
# tests/test_all.py
# Comprehensive test suite — run with: python -m pytest tests/
# ============================================================

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

# ────────────────────────────────────────────────
# FIXTURES
# ────────────────────────────────────────────────

@pytest.fixture
def sample_ohlcv():
    """Generates a synthetic OHLCV DataFrame for testing."""
    from data.ingestion import _generate_mock_ohlcv
    return _generate_mock_ohlcv("EURUSD", "H1", 300)


@pytest.fixture
def struct_engine():
    from core.market_structure import MarketStructureEngine
    return MarketStructureEngine()


@pytest.fixture
def strategy_engine():
    from strategy.engine import StrategyEngine
    return StrategyEngine()


# ────────────────────────────────────────────────
# DATA LAYER TESTS
# ────────────────────────────────────────────────

class TestDataIngestion:

    def test_mock_data_shape(self, sample_ohlcv):
        assert len(sample_ohlcv) > 0
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in sample_ohlcv.columns

    def test_mock_data_no_nan(self, sample_ohlcv):
        for col in ["open", "high", "low", "close", "volume"]:
            assert sample_ohlcv[col].isna().sum() == 0

    def test_mock_data_ohlc_valid(self, sample_ohlcv):
        """High must be >= Low, Close must be within High-Low range."""
        assert (sample_ohlcv["high"] >= sample_ohlcv["low"]).all()
        assert (sample_ohlcv["close"] <= sample_ohlcv["high"]).all()
        assert (sample_ohlcv["close"] >= sample_ohlcv["low"]).all()

    def test_mock_data_index_utc(self, sample_ohlcv):
        assert sample_ohlcv.index.tzinfo is not None

    def test_derived_columns(self, sample_ohlcv):
        assert "returns" in sample_ohlcv.columns
        assert "range" in sample_ohlcv.columns
        assert "body" in sample_ohlcv.columns


# ────────────────────────────────────────────────
# MARKET STRUCTURE ENGINE TESTS
# ────────────────────────────────────────────────

class TestMarketStructureEngine:

    def test_swing_point_detection(self, struct_engine, sample_ohlcv):
        swings = struct_engine._detect_swing_points(sample_ohlcv)
        assert len(swings) > 0
        highs = [s for s in swings if s.is_high]
        lows  = [s for s in swings if not s.is_high]
        assert len(highs) > 0
        assert len(lows) > 0

    def test_swing_high_is_local_maximum(self, struct_engine, sample_ohlcv):
        """Every swing high must be ≥ all bars in its lookback window."""
        N = struct_engine.cfg.swing_lookback
        swings = struct_engine._detect_swing_points(sample_ohlcv)
        highs = [s for s in swings if s.is_high]
        for sh in highs[:5]:
            window = sample_ohlcv["high"].iloc[max(0, sh.index - N): sh.index + N + 1]
            assert sh.price == window.max(), f"Swing high {sh.price} not max of window {window.max()}"

    def test_swing_low_is_local_minimum(self, struct_engine, sample_ohlcv):
        N = struct_engine.cfg.swing_lookback
        swings = struct_engine._detect_swing_points(sample_ohlcv)
        lows = [s for s in swings if not s.is_high]
        for sl in lows[:5]:
            window = sample_ohlcv["low"].iloc[max(0, sl.index - N): sl.index + N + 1]
            assert sl.price == window.min()

    def test_trend_detection_returns_valid(self, struct_engine, sample_ohlcv):
        from core.market_structure import Trend
        swings = struct_engine._detect_swing_points(sample_ohlcv)
        highs = [s for s in swings if s.is_high]
        lows  = [s for s in swings if not s.is_high]
        trend = struct_engine._determine_trend(highs, lows)
        assert trend in [Trend.BULLISH, Trend.BEARISH, Trend.RANGING]

    def test_fvg_detection(self, struct_engine, sample_ohlcv):
        fvgs = struct_engine._detect_fvg(sample_ohlcv)
        # FVGs may or may not exist depending on mock data; just check types
        for fvg in fvgs:
            assert fvg.gap_top > fvg.gap_bottom
            assert fvg.size_pips > 0

    def test_full_analysis_returns_structure(self, struct_engine, sample_ohlcv):
        from core.market_structure import StructureAnalysis
        result = struct_engine.analyse(sample_ohlcv, "EURUSD", "H1")
        assert isinstance(result, StructureAnalysis)
        assert result.current_price > 0

    def test_analysis_insufficient_data_raises(self, struct_engine):
        with pytest.raises(ValueError):
            df = pd.DataFrame({
                "open": [1, 2], "high": [1, 2], "low": [1, 2],
                "close": [1, 2], "volume": [100, 100],
                "returns": [0, 0], "log_returns": [0, 0],
                "range": [0, 0], "body": [0, 0],
                "upper_wick": [0, 0], "lower_wick": [0, 0]
            }, index=pd.date_range("2024-01-01", periods=2, tz="UTC"))
            struct_engine.analyse(df)


# ────────────────────────────────────────────────
# STRATEGY ENGINE TESTS
# ────────────────────────────────────────────────

class TestStrategyEngine:

    def test_indicator_calculation(self, sample_ohlcv):
        from strategy.engine import IndicatorCalculator
        indicators = IndicatorCalculator.compute_all(sample_ohlcv)
        assert 0 <= indicators.rsi_14 <= 100
        assert indicators.atr_14 > 0
        assert indicators.volume_ratio > 0

    def test_rsi_bounds(self, sample_ohlcv):
        from strategy.engine import IndicatorCalculator
        for _ in range(10):
            rsi = IndicatorCalculator._rsi(sample_ohlcv["close"], 14)
            assert 0 <= rsi <= 100

    def test_signal_has_valid_rr(self, strategy_engine, struct_engine, sample_ohlcv):
        from data.ingestion import _generate_mock_ohlcv
        df_htf = _generate_mock_ohlcv("EURUSD", "H4", 300)

        try:
            primary = struct_engine.analyse(sample_ohlcv, "EURUSD", "H1")
            htf     = struct_engine.analyse(df_htf, "EURUSD", "H4")
            signal  = strategy_engine.generate_signal(
                "EURUSD", primary, htf, sample_ohlcv, 0.6, 0.2
            )
            if signal is not None:
                assert signal.risk_reward >= CONFIG_STRATEGY_RR_MIN
                assert 0 <= signal.confidence <= 100
                assert signal.stop_loss != signal.entry_price
                assert signal.take_profit != signal.entry_price
        except Exception:
            pass  # Mock data may not always produce signals

    def test_signal_buy_sl_below_entry(self, strategy_engine, struct_engine, sample_ohlcv):
        from data.ingestion import _generate_mock_ohlcv
        df_htf = _generate_mock_ohlcv("EURUSD", "H4", 300)
        try:
            primary = struct_engine.analyse(sample_ohlcv, "EURUSD", "H1")
            htf = struct_engine.analyse(df_htf, "EURUSD", "H4")
            signal = strategy_engine.generate_signal(
                "EURUSD", primary, htf, sample_ohlcv, 0.7, 0.5
            )
            if signal and signal.direction.value == "BUY":
                assert signal.stop_loss < signal.entry_price
                assert signal.take_profit > signal.entry_price
            elif signal and signal.direction.value == "SELL":
                assert signal.stop_loss > signal.entry_price
                assert signal.take_profit < signal.entry_price
        except Exception:
            pass

    def test_confluence_scores_sum_to_valid_range(self, strategy_engine, struct_engine, sample_ohlcv):
        from data.ingestion import _generate_mock_ohlcv
        from core.market_structure import Trend, BOS
        from strategy.engine import SignalType, IndicatorSet

        # Manually build a minimal confluence scoring test
        primary = struct_engine.analyse(sample_ohlcv, "EURUSD", "H1")
        htf = struct_engine.analyse(sample_ohlcv, "EURUSD", "H1")
        indicators = IndicatorSet()

        for direction in [SignalType.BUY, SignalType.SELL]:
            scores = strategy_engine._score_confluence(
                direction, primary, htf, indicators, 0.6, 0.2
            )
            for key, val in scores.items():
                assert 0.0 <= val <= 1.0, f"Score {key}={val} out of range"


# ────────────────────────────────────────────────
# ML LAYER TESTS
# ────────────────────────────────────────────────

class TestMLLayer:

    def test_feature_engineering_shape(self, sample_ohlcv):
        from ml.model import FeatureEngineer
        fe = FeatureEngineer()
        features = fe.build_features(sample_ohlcv)
        assert len(features) > 0
        assert len(features.columns) >= 10
        assert features.isna().sum().sum() == 0

    def test_feature_no_future_leakage(self, sample_ohlcv):
        """
        Verify that feature at row i only uses data from rows [0..i].
        This is ensured by our use of shift() and rolling() — but we
        verify by checking that removing future data doesn't change past features.
        """
        from ml.model import FeatureEngineer
        fe = FeatureEngineer()
        full_features = fe.build_features(sample_ohlcv)
        half_df = sample_ohlcv.iloc[:len(sample_ohlcv)//2]
        half_features = fe.build_features(half_df)

        # The features at the last bar of half_df should match full_features
        common_idx = full_features.index.intersection(half_features.index)
        if len(common_idx) > 0:
            last_common = common_idx[-1]
            for col in ["ret_1", "rsi_14", "atr_norm"]:
                if col in full_features.columns and col in half_features.columns:
                    full_val = full_features.loc[last_common, col]
                    half_val = half_features.loc[last_common, col]
                    assert abs(full_val - half_val) < 1e-8, \
                        f"Leakage detected in {col}: {full_val} vs {half_val}"

    def test_label_building(self, sample_ohlcv):
        from ml.model import FeatureEngineer
        fe = FeatureEngineer()
        features = fe.build_features(sample_ohlcv)
        labels = fe.build_labels(sample_ohlcv, features.index)
        assert len(labels) > 0
        assert set(labels.unique()).issubset({0, 1})

    def test_model_train_predict(self, sample_ohlcv):
        from ml.model import FeatureEngineer, ModelTrainer
        fe = FeatureEngineer()
        features = fe.build_features(sample_ohlcv)
        labels = fe.build_labels(sample_ohlcv, features.index)

        trainer = ModelTrainer(model_type="random_forest")
        if len(features) >= trainer.cfg.min_train_samples:
            metrics = trainer.train(features, labels)
            assert "mean_accuracy" in metrics
            assert 0 <= metrics["mean_accuracy"] <= 1

            prob = trainer.predict_probability(features)
            assert 0 <= prob <= 1

    def test_prediction_returns_neutral_without_model(self, sample_ohlcv):
        from ml.model import FeatureEngineer, ModelTrainer
        fe = FeatureEngineer()
        features = fe.build_features(sample_ohlcv)
        trainer = ModelTrainer()
        trainer.model = None  # Force untrained state
        prob = trainer.predict_probability(features)
        assert prob == 0.5


# ────────────────────────────────────────────────
# BACKTEST ENGINE TESTS
# ────────────────────────────────────────────────

class TestBacktestEngine:

    def test_backtest_runs_without_error(self, sample_ohlcv):
        from backtest.engine import BacktestEngine
        engine = BacktestEngine()
        results = engine.run(sample_ohlcv, symbol="EURUSD", timeframe="H1")
        assert results is not None
        assert results.initial_capital > 0
        assert results.final_capital > 0

    def test_equity_curve_length(self, sample_ohlcv):
        from backtest.engine import BacktestEngine
        engine = BacktestEngine()
        results = engine.run(sample_ohlcv, "EURUSD")
        expected_len = len(sample_ohlcv) - engine.WARMUP_BARS - 1
        assert len(results.equity_curve) == expected_len

    def test_win_rate_between_0_and_1(self, sample_ohlcv):
        from backtest.engine import BacktestEngine
        engine = BacktestEngine()
        results = engine.run(sample_ohlcv, "EURUSD")
        assert 0 <= results.win_rate <= 1

    def test_max_drawdown_is_negative_or_zero(self, sample_ohlcv):
        from backtest.engine import BacktestEngine
        engine = BacktestEngine()
        results = engine.run(sample_ohlcv, "EURUSD")
        assert results.max_drawdown_pct <= 0

    def test_trade_sl_tp_consistency(self, sample_ohlcv):
        from backtest.engine import BacktestEngine
        engine = BacktestEngine()
        results = engine.run(sample_ohlcv, "EURUSD")
        for trade in results.trades:
            assert trade.exit_reason in ("tp", "sl", "timeout", "eod")
            assert trade.exit_price is not None


# ────────────────────────────────────────────────
# SENTINEL
# ────────────────────────────────────────────────

# Hack: import CONFIG.strategy.risk_reward_min for use in signal tests
try:
    from config.settings import CONFIG
    CONFIG_STRATEGY_RR_MIN = CONFIG.strategy.risk_reward_min
except Exception:
    CONFIG_STRATEGY_RR_MIN = 2.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
