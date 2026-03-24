# ============================================================
# config/settings.py
# Central configuration — all tuneable parameters live here.
# Load secrets from .env; never hard-code credentials.
# ============================================================

import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import List, Optional

load_dotenv()


# ────────────────────────────────────────────────
# 1. BROKER / EXCHANGE CREDENTIALS
# ────────────────────────────────────────────────
class BrokerConfig(BaseModel):
    # MT5 (Forex / CFDs)
    mt5_login: int = int(os.getenv("MT5_LOGIN", "0"))
    mt5_password: str = os.getenv("MT5_PASSWORD", "")
    mt5_server: str = os.getenv("MT5_SERVER", "")

    # Crypto (via CCXT)
    exchange_id: str = os.getenv("EXCHANGE_ID", "binance")       # binance | bybit | okx
    api_key: str = os.getenv("EXCHANGE_API_KEY", "")
    api_secret: str = os.getenv("EXCHANGE_API_SECRET", "")
    use_testnet: bool = os.getenv("USE_TESTNET", "true").lower() == "true"


# ────────────────────────────────────────────────
# 2. MARKET / INSTRUMENT CONFIG
# ────────────────────────────────────────────────
class MarketConfig(BaseModel):
    symbols: List[str] = ["EURUSD", "GBPUSD", "BTCUSDT", "ETHUSDT"]
    primary_timeframe: str = "H1"          # H1 = 1-hour candles
    higher_timeframe: str = "H4"           # For trend bias confirmation
    lower_timeframe: str = "M15"           # For entry precision
    candle_limit: int = 500                # Historical candles to fetch


# ────────────────────────────────────────────────
# 3. MARKET STRUCTURE ENGINE PARAMETERS
# ────────────────────────────────────────────────
class StructureConfig(BaseModel):
    swing_lookback: int = 5        # Bars each side to confirm swing high/low
    fvg_min_size_pips: float = 5.0 # Minimum FVG size to be tradeable
    bos_confirmation_bars: int = 1 # Bars after break to confirm BOS
    choch_lookback: int = 20       # Bars to look back for CHoCH detection
    liquidity_zone_threshold: float = 0.0005  # 5 pips proximity = same zone


# ────────────────────────────────────────────────
# 4. STRATEGY / SIGNAL CONFIG
# ────────────────────────────────────────────────
class StrategyConfig(BaseModel):
    min_confidence_score: float = 65.0     # Minimum % to emit a signal
    risk_reward_min: float = 2.0           # Minimum R:R ratio
    max_open_trades: int = 3
    risk_per_trade_pct: float = 1.0        # % of account at risk per trade
    confluence_weights: dict = {
        "market_structure": 0.35,
        "fvg_present": 0.20,
        "liquidity_sweep": 0.15,
        "htf_trend_aligned": 0.15,
        "sentiment_score": 0.10,
        "ml_probability": 0.05,
    }


# ────────────────────────────────────────────────
# 5. ML LAYER CONFIG
# ────────────────────────────────────────────────
class MLConfig(BaseModel):
    model_type: str = "xgboost"            # xgboost | lgbm | lstm | random_forest
    lookback_window: int = 50              # Feature window (bars)
    train_test_split: float = 0.8
    retrain_interval_hours: int = 24
    min_train_samples: int = 500
    features: List[str] = [
        "returns_1", "returns_3", "returns_5",
        "atr_14", "rsi_14", "adx_14",
        "bb_width", "volume_ratio",
        "bos_signal", "choch_signal", "fvg_bull", "fvg_bear",
        "liq_zone_proximity", "htf_trend",
        "sentiment_score",
    ]
    target_horizon_bars: int = 5           # Predict move over next N bars
    target_threshold_pct: float = 0.5     # % move to label as 1 (bullish)


# ────────────────────────────────────────────────
# 6. SENTIMENT / NEWS CONFIG
# ────────────────────────────────────────────────
class SentimentConfig(BaseModel):
    newsapi_key: str = os.getenv("NEWSAPI_KEY", "")
    use_finbert: bool = True               # Use FinBERT or fall back to VADER
    rss_feeds: List[str] = [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://rss.forexlive.com/rss/eo/forex",
    ]
    sentiment_decay_hours: float = 6.0    # Sentiment loses weight after N hours
    high_impact_keywords: List[str] = [
        "rate hike", "rate cut", "inflation", "CPI", "NFP",
        "Fed", "ECB", "war", "sanctions", "recession", "default",
    ]


# ────────────────────────────────────────────────
# 7. ALERT CONFIG
# ────────────────────────────────────────────────
class AlertConfig(BaseModel):
    # SMS via Twilio
    twilio_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from: str = os.getenv("TWILIO_FROM_NUMBER", "")
    alert_phone: str = os.getenv("ALERT_PHONE_NUMBER", "")

    # Email via SendGrid
    sendgrid_api_key: str = os.getenv("SENDGRID_API_KEY", "")
    email_from: str = os.getenv("EMAIL_FROM", "bot@tradingbot.com")
    email_to: str = os.getenv("EMAIL_TO", "")

    min_score_for_alert: float = 70.0     # Only alert on high-confidence signals
    cooldown_minutes: int = 60            # Avoid duplicate alerts per symbol


# ────────────────────────────────────────────────
# 8. BACKTESTING CONFIG
# ────────────────────────────────────────────────
class BacktestConfig(BaseModel):
    initial_capital: float = 10_000.0
    commission_pct: float = 0.001          # 0.1% per trade (typical crypto)
    spread_pips: float = 1.5               # Typical FX spread
    slippage_pips: float = 0.5
    start_date: str = "2022-01-01"
    end_date: str = "2024-12-31"


# ────────────────────────────────────────────────
# 9. MASTER CONFIG
# ────────────────────────────────────────────────
class TradingBotConfig(BaseModel):
    broker: BrokerConfig = BrokerConfig()
    market: MarketConfig = MarketConfig()
    structure: StructureConfig = StructureConfig()
    strategy: StrategyConfig = StrategyConfig()
    ml: MLConfig = MLConfig()
    sentiment: SentimentConfig = SentimentConfig()
    alerts: AlertConfig = AlertConfig()
    backtest: BacktestConfig = BacktestConfig()
    log_level: str = "INFO"
    data_dir: str = "./data/cache"
    model_dir: str = "./models"


# Singleton — import this throughout the project
CONFIG = TradingBotConfig()
