# ============================================================
# data/ingestion.py
# Unified data ingestion layer.
# Supports: MT5 (forex/CFD) and CCXT (crypto exchanges).
# Returns clean, standardised pandas DataFrames.
# ============================================================

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import CONFIG


# ────────────────────────────────────────────────
# TIMEFRAME MAPPINGS
# ────────────────────────────────────────────────
MT5_TF_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408,
}

CCXT_TF_MAP = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


# ────────────────────────────────────────────────
# BASE CLASS
# ────────────────────────────────────────────────
class BaseDataProvider(ABC):
    """
    Abstract base for all data providers.
    Every provider must implement fetch_ohlcv and subscribe_ticks.
    """

    def __init__(self):
        self.connected = False

    @abstractmethod
    def connect(self) -> bool:
        pass

    @abstractmethod
    def disconnect(self):
        pass

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """
        Returns a DataFrame with columns:
          [timestamp, open, high, low, close, volume]
        Index: DatetimeIndex (UTC)
        """
        pass

    @staticmethod
    def _validate_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
        """
        Validates and cleans raw OHLCV data.
        - Drops rows with NaN in OHLCV columns
        - Removes duplicate timestamps
        - Sorts ascending by time
        - Ensures correct dtypes
        """
        df = df.copy()

        # Drop any rows missing core price data
        df.dropna(subset=REQUIRED_COLUMNS, inplace=True)

        # Remove duplicates, keep last
        df = df[~df.index.duplicated(keep="last")]

        # Sort ascending (oldest first)
        df.sort_index(inplace=True)

        # Enforce dtypes
        for col in REQUIRED_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Sanity checks: high >= low, close within high/low range
        invalid_mask = (
            (df["high"] < df["low"]) |
            (df["close"] > df["high"]) |
            (df["close"] < df["low"]) |
            (df["open"] > df["high"]) |
            (df["open"] < df["low"])
        )
        if invalid_mask.sum() > 0:
            logger.warning(f"Dropping {invalid_mask.sum()} malformed candles")
            df = df[~invalid_mask]

        # Add derived columns used throughout the system
        df["returns"] = df["close"].pct_change()
        df["log_returns"] = np.log(df["close"] / df["close"].shift(1))
        df["range"] = df["high"] - df["low"]
        df["body"] = abs(df["close"] - df["open"])
        df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
        df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]

        return df.dropna(subset=["returns"])


# ────────────────────────────────────────────────
# MT5 PROVIDER (Forex / CFD)
# ────────────────────────────────────────────────
class MT5DataProvider(BaseDataProvider):
    """
    MetaTrader 5 data provider.
    NOTE: mt5 package requires Windows + MT5 terminal installed.
    On non-Windows systems this falls back to mock data automatically.
    """

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
            result = mt5.initialize(
                login=CONFIG.broker.mt5_login,
                password=CONFIG.broker.mt5_password,
                server=CONFIG.broker.mt5_server,
            )
            if result:
                info = mt5.terminal_info()
                logger.info(f"MT5 connected: {info.name}")
                self.connected = True
            else:
                logger.error(f"MT5 init failed: {mt5.last_error()}")
            return result
        except ImportError:
            logger.warning("MetaTrader5 not available — using mock provider")
            self.connected = False
            return False

    def disconnect(self):
        if self.connected:
            self._mt5.shutdown()
            self.connected = False
            logger.info("MT5 disconnected")

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since: Optional[datetime] = None,
    ) -> pd.DataFrame:

        if not self.connected:
            logger.warning("MT5 not connected — returning mock data")
            return _generate_mock_ohlcv(symbol, timeframe, limit)

        tf_code = MT5_TF_MAP.get(timeframe, MT5_TF_MAP["H1"])
        rates = self._mt5.copy_rates_from_pos(symbol, tf_code, 0, limit)

        if rates is None or len(rates) == 0:
            logger.error(f"MT5: No data for {symbol} {timeframe}")
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("timestamp", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        df = df[REQUIRED_COLUMNS]

        return self._validate_ohlcv(df)


# ────────────────────────────────────────────────
# CCXT PROVIDER (Crypto Exchanges)
# ────────────────────────────────────────────────
class CCXTDataProvider(BaseDataProvider):
    """
    Crypto exchange provider via CCXT unified API.
    Supports: Binance, Bybit, OKX, Coinbase, Kraken, etc.
    """

    def __init__(self):
        super().__init__()
        self._exchange = None

    def connect(self) -> bool:
        try:
            import ccxt
            exchange_class = getattr(ccxt, CONFIG.broker.exchange_id)
            params = {
                "apiKey": CONFIG.broker.api_key,
                "secret": CONFIG.broker.api_secret,
                "enableRateLimit": True,    # Respect rate limits automatically
            }
            if CONFIG.broker.use_testnet:
                params["options"] = {"defaultType": "future"}

            self._exchange = exchange_class(params)

            if CONFIG.broker.use_testnet:
                self._exchange.set_sandbox_mode(True)

            # Test connectivity
            self._exchange.load_markets()
            logger.info(f"CCXT connected to {CONFIG.broker.exchange_id}")
            self.connected = True
            return True

        except Exception as e:
            logger.error(f"CCXT connection failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        self.connected = False
        self._exchange = None

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since: Optional[datetime] = None,
    ) -> pd.DataFrame:

        if not self.connected:
            logger.warning("CCXT not connected — returning mock data")
            return _generate_mock_ohlcv(symbol, timeframe, limit)

        ccxt_tf = CCXT_TF_MAP.get(timeframe, "1h")
        since_ms = int(since.timestamp() * 1000) if since else None

        try:
            raw = self._exchange.fetch_ohlcv(
                symbol, ccxt_tf, since=since_ms, limit=limit
            )
        except Exception as e:
            logger.error(f"CCXT fetch error for {symbol}: {e}")
            return pd.DataFrame()

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)

        return self._validate_ohlcv(df)

    async def fetch_ohlcv_async(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Async wrapper for non-blocking data fetching."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.fetch_ohlcv, symbol, timeframe, limit
        )


# ────────────────────────────────────────────────
# MOCK DATA GENERATOR (Testing / CI)
# ────────────────────────────────────────────────
def _generate_mock_ohlcv(symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
    """
    Generates realistic synthetic OHLCV data using a random walk
    with volatility clustering — suitable for unit testing and CI.
    """
    logger.info(f"Generating mock data for {symbol} {timeframe} ({limit} bars)")

    np.random.seed(42)
    freq_map = {
        "M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
        "H1": "1h", "H4": "4h", "D1": "1D",
    }
    freq = freq_map.get(timeframe, "1h")
    end = pd.Timestamp.now(tz="UTC").floor("h")
    idx = pd.date_range(end=end, periods=limit, freq=freq, tz="UTC")

    # Geometric Brownian Motion with GARCH-like vol clustering
    returns = np.random.normal(0, 0.001, limit)
    vol = np.ones(limit) * 0.001
    for i in range(1, limit):
        vol[i] = np.sqrt(0.9 * vol[i-1]**2 + 0.1 * returns[i-1]**2 + 1e-8)
        returns[i] = np.random.normal(0, vol[i])

    base_price = 1.1000 if "USD" in symbol and len(symbol) == 6 else 30000.0
    close = base_price * np.cumprod(1 + returns)

    # Build OHLCV from close prices — ensure strict OHLC validity
    open_ = np.roll(close, 1)
    open_[0] = close[0]

    # High = max(open, close) + positive noise
    high_noise = np.abs(np.random.normal(0, 0.0005, limit)) * close
    high = np.maximum(open_, close) + high_noise

    # Low = min(open, close) - positive noise
    low_noise = np.abs(np.random.normal(0, 0.0005, limit)) * close
    low = np.minimum(open_, close) - low_noise

    volume = np.random.lognormal(10, 1, limit)

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    }, index=idx)

    return BaseDataProvider._validate_ohlcv(df)


# ────────────────────────────────────────────────
# DATA MANAGER — orchestrates providers + caching
# ────────────────────────────────────────────────
class DataManager:
    """
    High-level interface that:
    1. Routes requests to MT5 or CCXT based on symbol type
    2. Caches fetched data to parquet files
    3. Provides multi-timeframe data bundles for the strategy engine
    """

    def __init__(self, provider: Optional[BaseDataProvider] = None):
        self.provider = provider or self._auto_detect_provider()
        import os
        os.makedirs(CONFIG.data_dir, exist_ok=True)

    @staticmethod
    def _auto_detect_provider() -> BaseDataProvider:
        """Picks provider based on config or available packages."""
        exchange = CONFIG.broker.exchange_id
        if exchange and exchange != "":
            p = CCXTDataProvider()
            if p.connect():
                return p
        p2 = MT5DataProvider()
        if p2.connect():
            return p2
        # Fallback: return CCXT in disconnected mode (uses mock data)
        logger.warning("No live provider available — using mock data")
        return CCXTDataProvider()

    def get_mtf_data(self, symbol: str) -> dict:
        """
        Returns a dict of DataFrames for multiple timeframes:
        {
          "H1":  DataFrame,   # Primary analysis timeframe
          "H4":  DataFrame,   # Higher timeframe trend bias
          "M15": DataFrame,   # Entry precision timeframe
        }
        All DataFrames are validated and aligned.
        """
        result = {}
        timeframes = [
            CONFIG.market.primary_timeframe,
            CONFIG.market.higher_timeframe,
            CONFIG.market.lower_timeframe,
        ]
        for tf in timeframes:
            cache_path = f"{CONFIG.data_dir}/{symbol}_{tf}.parquet"
            try:
                # Attempt to load cached data < 5 minutes old
                import os
                if os.path.exists(cache_path):
                    age = time.time() - os.path.getmtime(cache_path)
                    if age < 300:  # 5 minutes
                        df = pd.read_parquet(cache_path)
                        result[tf] = df
                        continue

                df = self.provider.fetch_ohlcv(
                    symbol, tf, CONFIG.market.candle_limit
                )
                if not df.empty:
                    df.to_parquet(cache_path)
                    result[tf] = df
                    logger.debug(f"Fetched {len(df)} candles: {symbol} {tf}")
            except Exception as e:
                logger.error(f"Failed to fetch {symbol} {tf}: {e}")

        return result

    def get_latest_candle(self, symbol: str, timeframe: str) -> Optional[pd.Series]:
        """Returns the most recent closed candle."""
        df = self.provider.fetch_ohlcv(symbol, timeframe, limit=2)
        if df.empty:
            return None
        return df.iloc[-1]  # Last completed candle
