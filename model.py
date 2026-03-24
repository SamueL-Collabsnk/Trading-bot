# ============================================================
# ml/model.py
#
# Machine Learning Layer
# ──────────────────────────────────────────────────────────
# Provides:
#  1. Feature engineering from OHLCV + structure signals
#  2. Binary classification (price goes up/down in N bars)
#  3. XGBoost / LightGBM / RandomForest training pipeline
#  4. SHAP-based model explainability
#  5. Walk-forward validation (avoids look-ahead bias)
#  6. Automatic retraining scheduler
# ============================================================

import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.metrics import (accuracy_score, classification_report,
                             precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

from config.settings import CONFIG


# ────────────────────────────────────────────────
# FEATURE ENGINEERING
# ────────────────────────────────────────────────

class FeatureEngineer:
    """
    Transforms raw OHLCV + structure signals into an ML feature matrix.

    ALL FEATURES are computed using only past data (no future leakage).
    Each feature at row i uses only data from rows [0..i].

    Features fall into 4 categories:
    A) Price-action features  — returns, momentum, volatility
    B) Technical indicators   — trend, oscillators
    C) Market structure       — BOS/CHoCH/FVG signals (binary flags)
    D) Regime features        — volatility regime, market efficiency ratio
    """

    def __init__(self):
        self.cfg = CONFIG.ml

    def build_features(
        self,
        df: pd.DataFrame,
        structure_signals: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Builds the full feature matrix.

        Args:
            df: OHLCV DataFrame (from DataManager)
            structure_signals: Optional DataFrame with structure event columns
                               (bos_bull, bos_bear, choch_bull, choch_bear, fvg_bull, fvg_bear)

        Returns:
            Feature DataFrame with NaN rows dropped.
        """
        features = pd.DataFrame(index=df.index)

        # ── A. Price-Action Features ──────────────────────
        # Returns at multiple horizons (captures momentum)
        for n in [1, 3, 5, 10, 20]:
            features[f"ret_{n}"] = df["close"].pct_change(n)

        # Log returns (Gaussian-distributed, better for ML)
        features["log_ret_1"] = np.log(df["close"] / df["close"].shift(1))

        # OHLC ratios (candle shape information)
        features["body_ratio"] = (df["close"] - df["open"]).abs() / (df["high"] - df["low"] + 1e-10)
        features["upper_wick_ratio"] = (df["high"] - df[["open","close"]].max(axis=1)) / (df["high"] - df["low"] + 1e-10)
        features["lower_wick_ratio"] = (df[["open","close"]].min(axis=1) - df["low"]) / (df["high"] - df["low"] + 1e-10)

        # Candle direction: 1=bullish, -1=bearish
        features["candle_dir"] = np.sign(df["close"] - df["open"])

        # ── B. Technical Indicator Features ───────────────

        # RSI (momentum oscillator)
        features["rsi_14"] = self._rsi(df["close"], 14)
        features["rsi_7"]  = self._rsi(df["close"], 7)

        # RSI divergence proxy
        features["rsi_sma"] = features["rsi_14"] - features["rsi_14"].rolling(10).mean()

        # ATR (volatility)
        atr_14 = self._atr(df["high"], df["low"], df["close"], 14)
        features["atr_14"] = atr_14
        features["atr_norm"] = atr_14 / (df["close"] + 1e-10)  # Normalised ATR

        # EMA distance (trend strength)
        ema_20  = df["close"].ewm(span=20, adjust=False).mean()
        ema_50  = df["close"].ewm(span=50, adjust=False).mean()
        ema_200 = df["close"].ewm(span=200, adjust=False).mean()
        features["ema20_dist"]  = (df["close"] - ema_20) / (ema_20 + 1e-10)
        features["ema50_dist"]  = (df["close"] - ema_50) / (ema_50 + 1e-10)
        features["ema200_dist"] = (df["close"] - ema_200) / (ema_200 + 1e-10)
        features["ema_cross_2050"] = (ema_20 > ema_50).astype(int)

        # Bollinger Band features
        bb_mid = df["close"].rolling(20).mean()
        bb_std = df["close"].rolling(20).std()
        features["bb_position"] = (df["close"] - bb_mid) / (2 * bb_std + 1e-10)  # -1 to +1
        features["bb_width"] = (4 * bb_std) / (bb_mid + 1e-10)  # Volatility width

        # ADX (trend strength, 0-100)
        features["adx_14"] = self._adx(df["high"], df["low"], df["close"], 14)

        # Volume features
        vol_ma = df["volume"].rolling(20).mean()
        features["volume_ratio"] = df["volume"] / (vol_ma + 1e-10)
        features["volume_trend"] = vol_ma.pct_change(5)

        # ── C. Market Structure Features ──────────────────
        if structure_signals is not None and not structure_signals.empty:
            for col in ["bos_bull", "bos_bear", "choch_bull", "choch_bear", "fvg_bull", "fvg_bear"]:
                if col in structure_signals.columns:
                    features[col] = structure_signals[col].reindex(features.index).fillna(0)

            # Proximity to nearest FVG (normalised)
            if "fvg_level" in structure_signals.columns:
                fvg_level = structure_signals["fvg_level"].reindex(features.index)
                features["fvg_proximity"] = abs(df["close"] - fvg_level) / (df["close"] + 1e-10)
            
            if "liq_zone_level" in structure_signals.columns:
                liq_level = structure_signals["liq_zone_level"].reindex(features.index)
                features["liq_proximity"] = abs(df["close"] - liq_level) / (df["close"] + 1e-10)
        else:
            # Fill structure columns with zeros if not provided
            for col in ["bos_bull","bos_bear","choch_bull","choch_bear","fvg_bull","fvg_bear"]:
                features[col] = 0

        # ── D. Market Regime Features ──────────────────────

        # Volatility regime: high/low relative to historical
        features["vol_regime"] = (
            df["close"].pct_change().rolling(5).std() /
            (df["close"].pct_change().rolling(50).std() + 1e-10)
        )

        # Market Efficiency Ratio (MER): directional / total movement
        # MER near 1.0 = strongly trending; near 0 = choppy
        direction_move = (df["close"] - df["close"].shift(10)).abs()
        total_move = df["close"].diff().abs().rolling(10).sum()
        features["efficiency_ratio"] = direction_move / (total_move + 1e-10)

        # Autocorrelation of returns (mean-reversion indicator)
        features["ret_autocorr"] = df["close"].pct_change().rolling(20).apply(
            lambda x: pd.Series(x).autocorr(lag=1), raw=False
        )

        # Drop NaN rows caused by rolling windows
        features = features.replace([np.inf, -np.inf], np.nan)
        features.dropna(inplace=True)

        return features

    def build_labels(self, df: pd.DataFrame, features_index: pd.Index) -> pd.Series:
        """
        Creates binary classification labels:
          1 = price will be higher in N bars (bullish)
          0 = price will be lower or flat in N bars (bearish)

        Uses future returns, so labels are only valid for historical data.
        NEVER use future data in live trading.

        threshold = CONFIG.ml.target_threshold_pct (default: 0.5%)
        """
        horizon = self.cfg.target_horizon_bars
        threshold = self.cfg.target_threshold_pct / 100

        future_return = df["close"].shift(-horizon) / df["close"] - 1
        labels = (future_return > threshold).astype(int)

        aligned = labels.reindex(features_index).dropna()

        # Safety check: ensure at least 2 classes exist for training
        if aligned.nunique() < 2:
            # Fall back to median split if threshold produces single class
            median_ret = future_return.reindex(features_index).dropna().median()
            aligned = (future_return.reindex(features_index) > median_ret).astype(int).dropna()

        return aligned

    # ─── Private indicator helpers ────────────────────────

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / (loss + 1e-10)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(high, low, close, period=14) -> pd.Series:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _adx(high, low, close, period=14) -> pd.Series:
        up   = high.diff().clip(lower=0)
        down = (-low.diff()).clip(lower=0)
        tr   = pd.concat([high-low, (high-close.shift()).abs(),
                          (low-close.shift()).abs()], axis=1).max(axis=1)
        atr  = tr.rolling(period).mean()
        plus_di  = 100 * up.rolling(period).mean()  / (atr + 1e-10)
        minus_di = 100 * down.rolling(period).mean() / (atr + 1e-10)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
        return dx.rolling(period).mean()


# ────────────────────────────────────────────────
# MODEL TRAINER
# ────────────────────────────────────────────────

class ModelTrainer:
    """
    Trains, evaluates, and saves the ML prediction model.

    Uses Walk-Forward Validation to prevent look-ahead bias:
    ┌────────────────────────────────────────────┐
    │  Train        │ Test  │                    │
    │  Train              │ Test  │              │
    │  Train                    │ Test  │        │
    │  Train                          │ Test  │  │
    └────────────────────────────────────────────┘
    Each fold trains on past data and tests on the next unseen block.
    """

    def __init__(self, model_type: str = None):
        self.cfg = CONFIG.ml
        self.model_type = model_type or self.cfg.model_type
        self.model = None
        self.feature_names = None
        self.trained_at = None
        os.makedirs(CONFIG.model_dir, exist_ok=True)

    def _build_model(self):
        """Instantiates the model based on config."""
        if self.model_type == "xgboost":
            try:
                from xgboost import XGBClassifier
                return XGBClassifier(
                    n_estimators=300,
                    max_depth=5,
                    learning_rate=0.05,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    min_child_weight=5,   # Regularization — prevents overfitting
                    reg_alpha=0.1,        # L1 regularization
                    reg_lambda=1.0,       # L2 regularization
                    use_label_encoder=False,
                    eval_metric="logloss",
                    random_state=42,
                )
            except ImportError:
                logger.warning("XGBoost not available — falling back to GradientBoosting")

        if self.model_type == "lgbm":
            try:
                from lightgbm import LGBMClassifier
                return LGBMClassifier(
                    n_estimators=300,
                    max_depth=5,
                    learning_rate=0.05,
                    num_leaves=31,
                    subsample=0.8,
                    reg_alpha=0.1,
                    reg_lambda=1.0,
                    random_state=42,
                    verbose=-1,
                )
            except ImportError:
                logger.warning("LightGBM not available — falling back")

        # Default: GradientBoosting (no dependencies)
        return GradientBoostingClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=20,
            random_state=42,
        )

    def train(
        self,
        features: pd.DataFrame,
        labels: pd.Series,
    ) -> Dict:
        """
        Trains using walk-forward cross-validation.

        Returns performance metrics dict.
        """
        if len(features) < self.cfg.min_train_samples:
            raise ValueError(
                f"Insufficient training data: {len(features)} < {self.cfg.min_train_samples}"
            )

        # Align labels to features
        common_idx = features.index.intersection(labels.index)
        X = features.loc[common_idx]
        y = labels.loc[common_idx]

        logger.info(f"Training on {len(X)} samples, {X.shape[1]} features")
        logger.info(f"Label distribution: {y.value_counts().to_dict()}")

        self.feature_names = list(X.columns)

        # Walk-forward validation
        tscv = TimeSeriesSplit(n_splits=5)
        fold_metrics = []

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
            X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

            # Skip fold if either split has only one class (too small / imbalanced)
            if y_train.nunique() < 2 or y_val.nunique() < 2:
                logger.debug(f"Fold {fold+1}: skipping — single class in split")
                continue

            model = self._build_model()
            model.fit(X_train, y_train)

            y_pred = model.predict(X_val)
            y_prob = model.predict_proba(X_val)[:, 1]

            metrics = {
                "fold": fold + 1,
                "accuracy": accuracy_score(y_val, y_pred),
                "precision": precision_score(y_val, y_pred, zero_division=0),
                "recall": recall_score(y_val, y_pred, zero_division=0),
                "auc_roc": roc_auc_score(y_val, y_prob),
                "train_size": len(train_idx),
                "val_size": len(val_idx),
            }
            fold_metrics.append(metrics)
            logger.debug(f"Fold {fold+1}: ACC={metrics['accuracy']:.3f} "
                         f"AUC={metrics['auc_roc']:.3f} "
                         f"PREC={metrics['precision']:.3f}")

        if not fold_metrics:
            logger.warning("No valid folds — training on full data without CV metrics")
            fold_metrics = [{"accuracy": 0, "precision": 0, "recall": 0, "auc_roc": 0}]

        # Train final model on ALL data
        self.model = self._build_model()
        self.model.fit(X, y)
        self.trained_at = datetime.now(timezone.utc)

        # Aggregate metrics
        avg_metrics = {
            "mean_accuracy": np.mean([m["accuracy"] for m in fold_metrics]),
            "mean_precision": np.mean([m["precision"] for m in fold_metrics]),
            "mean_recall": np.mean([m["recall"] for m in fold_metrics]),
            "mean_auc": np.mean([m["auc_roc"] for m in fold_metrics]),
            "std_auc": np.std([m["auc_roc"] for m in fold_metrics]),
            "folds": fold_metrics,
            "n_samples": len(X),
            "n_features": X.shape[1],
            "trained_at": self.trained_at.isoformat(),
        }

        logger.info(
            f"Training complete: "
            f"Avg AUC={avg_metrics['mean_auc']:.3f}±{avg_metrics['std_auc']:.3f} "
            f"Accuracy={avg_metrics['mean_accuracy']:.3f}"
        )

        self._save_model()
        return avg_metrics

    def predict_probability(self, features: pd.DataFrame) -> float:
        """
        Returns P(bullish) for the most recent feature vector.
        Used by the strategy engine as the ml_probability score.

        Returns 0.5 (neutral) if model is not trained.
        """
        if self.model is None:
            if not self._load_model():
                return 0.5

        try:
            # Use only the last row (current bar)
            X = features[self.feature_names].iloc[[-1]]
            prob = self.model.predict_proba(X)[0][1]
            return float(prob)
        except Exception as e:
            logger.error(f"Prediction failed: {e}")
            return 0.5

    def explain_prediction(self, features: pd.DataFrame) -> dict:
        """
        Returns SHAP-based feature importance for the latest prediction.
        Makes the model's decision explainable (required for trust).
        """
        try:
            import shap
            explainer = shap.TreeExplainer(self.model)
            X = features[self.feature_names].iloc[[-1]]
            shap_values = explainer.shap_values(X)
            importance = dict(zip(self.feature_names, shap_values[0]))
            # Sort by absolute impact
            return dict(sorted(importance.items(), key=lambda x: abs(x[1]), reverse=True)[:10])
        except Exception as e:
            logger.warning(f"SHAP explanation failed: {e}")
            return {}

    def get_feature_importance(self) -> Dict[str, float]:
        """Returns built-in feature importance from the model."""
        if self.model is None or self.feature_names is None:
            return {}
        try:
            importance = self.model.feature_importances_
            return dict(sorted(
                zip(self.feature_names, importance),
                key=lambda x: x[1], reverse=True
            ))
        except Exception:
            return {}

    def _save_model(self):
        path = Path(CONFIG.model_dir) / f"model_{self.model_type}.pkl"
        with open(path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "feature_names": self.feature_names,
                "trained_at": self.trained_at,
                "model_type": self.model_type,
            }, f)
        logger.info(f"Model saved to {path}")

    def _load_model(self) -> bool:
        path = Path(CONFIG.model_dir) / f"model_{self.model_type}.pkl"
        if not path.exists():
            logger.warning(f"No saved model found at {path}")
            return False
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self.model = data["model"]
            self.feature_names = data["feature_names"]
            self.trained_at = data.get("trained_at")
            logger.info(f"Model loaded from {path} (trained: {self.trained_at})")
            return True
        except Exception as e:
            logger.error(f"Model load failed: {e}")
            return False

    def needs_retraining(self) -> bool:
        """Returns True if model is older than the configured interval."""
        if self.trained_at is None:
            return True
        age_hours = (
            datetime.now(timezone.utc) - self.trained_at
        ).total_seconds() / 3600
        return age_hours > self.cfg.retrain_interval_hours


# ────────────────────────────────────────────────
# TRAINING PIPELINE (convenience function)
# ────────────────────────────────────────────────

def train_model_pipeline(
    df: pd.DataFrame,
    structure_signals: Optional[pd.DataFrame] = None,
    model_type: str = None,
) -> Tuple[ModelTrainer, Dict]:
    """
    End-to-end training pipeline:
    1. Engineer features
    2. Build labels
    3. Train + validate
    4. Return trained trainer and metrics

    Usage:
        trainer, metrics = train_model_pipeline(df)
        prob = trainer.predict_probability(features)
    """
    engineer = FeatureEngineer()
    trainer = ModelTrainer(model_type=model_type)

    logger.info("Building features...")
    features = engineer.build_features(df, structure_signals)

    logger.info("Building labels...")
    labels = engineer.build_labels(df, features.index)

    logger.info("Training model...")
    metrics = trainer.train(features, labels)

    return trainer, metrics
