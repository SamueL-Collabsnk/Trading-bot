# ============================================================
# ml/sentiment.py
#
# News & Sentiment Analysis Pipeline
# ──────────────────────────────────────────────────────────
# Architecture:
#  1. News collection: NewsAPI + RSS feeds + FX-specific feeds
#  2. Primary scoring: FinBERT (finance-tuned BERT transformer)
#  3. Fallback scoring: VADER (lexicon-based, fast, no GPU needed)
#  4. Temporal decay: recent news weighs more than old news
#  5. Symbol mapping: news relevance scored per trading pair
#  6. Output: sentiment_score ∈ [-1.0, +1.0] per symbol
#
# Impact on trading decisions:
#  - score > +0.3  → bullish macro bias → favours BUY signals
#  - score < -0.3  → bearish macro bias → favours SELL signals
#  - |score| < 0.3 → neutral / avoid high-impact events
# ============================================================

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
from loguru import logger

from config.settings import CONFIG


# ────────────────────────────────────────────────
# DATA CLASSES
# ────────────────────────────────────────────────

@dataclass
class NewsItem:
    title: str
    description: str
    published_at: datetime
    source: str
    url: str
    raw_score: float = 0.0        # [-1, 1] from NLP model
    decayed_score: float = 0.0    # Score after temporal decay
    relevance: float = 1.0        # 0-1, symbol relevance weight
    is_high_impact: bool = False


# Symbol → relevant keywords mapping
SYMBOL_KEYWORDS = {
    "EURUSD": ["EUR", "euro", "ECB", "European", "eurozone", "EU"],
    "GBPUSD": ["GBP", "pound", "sterling", "Bank of England", "BOE", "UK", "Brexit"],
    "USDJPY": ["JPY", "yen", "BOJ", "Bank of Japan", "Japan"],
    "BTCUSDT": ["Bitcoin", "BTC", "crypto", "cryptocurrency", "blockchain", "halving"],
    "ETHUSDT": ["Ethereum", "ETH", "DeFi", "smart contract", "crypto"],
    "XAUUSD": ["gold", "XAU", "safe haven", "inflation hedge"],
}

# Universal keywords that affect all instruments
MACRO_KEYWORDS = [
    "Fed", "Federal Reserve", "FOMC", "rate hike", "rate cut",
    "inflation", "CPI", "NFP", "non-farm", "GDP", "recession",
    "war", "conflict", "sanctions", "tariff", "default",
    "oil", "energy", "geopolitical",
]

# High-impact event patterns
HIGH_IMPACT_PATTERNS = CONFIG.sentiment.high_impact_keywords


# ────────────────────────────────────────────────
# SCORER CLASSES
# ────────────────────────────────────────────────

class VaderSentimentScorer:
    """
    VADER-based fallback scorer.
    - No GPU required
    - Instant inference
    - Less accurate for financial jargon but very fast
    """

    def __init__(self):
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self.analyzer = SentimentIntensityAnalyzer()
            # Extend VADER's lexicon with finance-specific terms
            custom_lexicon = {
                "bullish": 2.5, "bearish": -2.5,
                "rally": 2.0, "crash": -3.0,
                "surge": 2.0, "plunge": -2.5,
                "hawkish": -1.0, "dovish": 1.5,   # Context: hawkish Fed = bearish bonds
                "rate hike": -1.5, "rate cut": 1.5,
                "default": -3.0, "bankruptcy": -3.0,
                "stimulus": 2.0, "austerity": -1.5,
                "breakout": 2.0, "breakdown": -2.0,
            }
            self.analyzer.lexicon.update(custom_lexicon)
            logger.debug("VADER scorer initialised")
        except ImportError:
            logger.warning("vaderSentiment not installed — scoring disabled")
            self.analyzer = None

    def score(self, text: str) -> float:
        """Returns compound score: -1 (very negative) to +1 (very positive)."""
        if self.analyzer is None:
            return 0.0
        scores = self.analyzer.polarity_scores(text)
        return scores["compound"]


class FinBERTScorer:
    """
    FinBERT-based primary scorer.
    - Finance-domain BERT model (fine-tuned on financial news)
    - Significantly more accurate than VADER for market news
    - Requires transformers + torch (GPU optional but recommended)

    Model: "ProsusAI/finbert" from HuggingFace
    Output: {positive, negative, neutral} → mapped to [-1, +1]
    """

    def __init__(self):
        self.pipeline = None
        self._load_model()

    def _load_model(self):
        try:
            from transformers import pipeline as hf_pipeline
            logger.info("Loading FinBERT model (first load may take a moment)...")
            self.pipeline = hf_pipeline(
                "text-classification",
                model="ProsusAI/finbert",
                tokenizer="ProsusAI/finbert",
                max_length=512,
                truncation=True,
                device=-1,    # -1 = CPU; set to 0 for GPU
            )
            logger.info("FinBERT loaded successfully")
        except Exception as e:
            logger.warning(f"FinBERT load failed ({e}) — falling back to VADER")
            self.pipeline = None

    def score(self, text: str) -> float:
        """
        Returns score in [-1.0, +1.0]:
          positive → +confidence
          negative → -confidence
          neutral  → 0.0
        """
        if self.pipeline is None:
            return 0.0
        try:
            result = self.pipeline(text[:512])[0]   # Limit to 512 tokens
            label = result["label"].lower()
            conf  = result["score"]                 # 0..1 confidence
            if label == "positive":
                return conf
            elif label == "negative":
                return -conf
            else:
                return 0.0
        except Exception as e:
            logger.error(f"FinBERT scoring error: {e}")
            return 0.0


# ────────────────────────────────────────────────
# NEWS COLLECTOR
# ────────────────────────────────────────────────

class NewsCollector:
    """Fetches news from NewsAPI and RSS feeds."""

    def __init__(self):
        self.cfg = CONFIG.sentiment
        self._cache: List[NewsItem] = []
        self._cache_time: Optional[datetime] = None

    def fetch_news(self, max_age_hours: float = 24.0) -> List[NewsItem]:
        """
        Returns news items from the past max_age_hours.
        Uses cache if data is < 15 minutes old.
        """
        if (self._cache_time and
                (datetime.now(timezone.utc) - self._cache_time).seconds < 900):
            return self._cache

        items = []
        items.extend(self._fetch_newsapi(max_age_hours))
        items.extend(self._fetch_rss(max_age_hours))

        # Deduplicate by title similarity
        items = self._deduplicate(items)
        self._cache = items
        self._cache_time = datetime.now(timezone.utc)
        logger.info(f"Fetched {len(items)} news items")
        return items

    def _fetch_newsapi(self, max_age_hours: float) -> List[NewsItem]:
        """Fetches from NewsAPI (requires API key)."""
        if not self.cfg.newsapi_key:
            return []
        try:
            from newsapi import NewsApiClient
            client = NewsApiClient(api_key=self.cfg.newsapi_key)
            from_time = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours))
            response = client.get_everything(
                q="forex OR crypto OR bitcoin OR Federal Reserve OR ECB OR interest rate",
                from_param=from_time.strftime("%Y-%m-%dT%H:%M:%S"),
                language="en",
                sort_by="publishedAt",
                page_size=50,
            )
            items = []
            for art in response.get("articles", []):
                if art["title"] and art["publishedAt"]:
                    pub = datetime.fromisoformat(
                        art["publishedAt"].replace("Z", "+00:00")
                    )
                    items.append(NewsItem(
                        title=art["title"],
                        description=art.get("description", "") or "",
                        published_at=pub,
                        source=art.get("source", {}).get("name", "Unknown"),
                        url=art.get("url", ""),
                    ))
            return items
        except Exception as e:
            logger.warning(f"NewsAPI fetch failed: {e}")
            return []

    def _fetch_rss(self, max_age_hours: float) -> List[NewsItem]:
        """Fetches from configured RSS feeds."""
        try:
            import feedparser
        except ImportError:
            return []

        items = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

        for url in self.cfg.rss_feeds:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:20]:
                    pub = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        import calendar
                        pub = datetime.fromtimestamp(
                            calendar.timegm(entry.published_parsed), tz=timezone.utc
                        )
                    if pub and pub > cutoff:
                        items.append(NewsItem(
                            title=entry.get("title", ""),
                            description=entry.get("summary", ""),
                            published_at=pub,
                            source=feed.feed.get("title", url),
                            url=entry.get("link", ""),
                        ))
            except Exception as e:
                logger.warning(f"RSS fetch failed ({url}): {e}")

        return items

    @staticmethod
    def _deduplicate(items: List[NewsItem]) -> List[NewsItem]:
        """Remove near-duplicate headlines using simple word overlap."""
        seen_titles = set()
        unique = []
        for item in items:
            # Normalise title to detect near-dupes
            key = frozenset(item.title.lower().split()[:8])
            if key not in seen_titles:
                seen_titles.add(key)
                unique.append(item)
        return unique


# ────────────────────────────────────────────────
# MAIN SENTIMENT ANALYSER
# ────────────────────────────────────────────────

class SentimentAnalyser:
    """
    Complete pipeline: collect → score → decay → aggregate.

    Usage:
        analyser = SentimentAnalyser()
        scores = analyser.get_scores()
        # → {"EURUSD": 0.32, "BTCUSDT": -0.15, ...}
    """

    def __init__(self):
        self.cfg = CONFIG.sentiment
        self.collector = NewsCollector()
        self.vader = VaderSentimentScorer()
        self.finbert = FinBERTScorer() if self.cfg.use_finbert else None

    def get_scores(
        self,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """
        Returns a dict of {symbol: sentiment_score} where:
          score ∈ [-1.0, +1.0]
          +1.0 = strongly bullish
          -1.0 = strongly bearish
           0.0 = neutral
        """
        symbols = symbols or CONFIG.market.symbols
        news = self.collector.fetch_news(max_age_hours=24)

        if not news:
            logger.warning("No news items available — returning neutral sentiment")
            return {s: 0.0 for s in symbols}

        # Score and annotate each news item
        scored_news = [self._score_item(item) for item in news]

        # Aggregate per symbol
        result = {}
        for symbol in symbols:
            result[symbol] = self._aggregate_for_symbol(scored_news, symbol)

        logger.debug(f"Sentiment scores: {result}")
        return result

    def _score_item(self, item: NewsItem) -> NewsItem:
        """
        Scores a single news item using FinBERT (or VADER fallback).
        Applies temporal decay.
        """
        text = f"{item.title}. {item.description}"

        # Primary: FinBERT
        if self.finbert and self.finbert.pipeline:
            item.raw_score = self.finbert.score(text)
        else:
            item.raw_score = self.vader.score(text)

        # Temporal decay:
        # score_decayed = score × exp(-λt)
        # where λ = ln(2) / half_life
        # At t=0: full weight; at t=half_life: 50% weight
        age_hours = max(0, (
            datetime.now(timezone.utc) - item.published_at
        ).total_seconds() / 3600)
        half_life = self.cfg.sentiment_decay_hours
        decay_factor = np.exp(-np.log(2) * age_hours / half_life)
        item.decayed_score = item.raw_score * decay_factor

        # Flag high-impact events (override neutral scores)
        full_text = text.lower()
        item.is_high_impact = any(
            kw.lower() in full_text for kw in HIGH_IMPACT_PATTERNS
        )

        return item

    def _aggregate_for_symbol(
        self,
        news: List[NewsItem],
        symbol: str,
    ) -> float:
        """
        Computes a single sentiment score for a symbol by:
        1. Weighting each news item by its relevance to the symbol
        2. Applying extra weight to high-impact events
        3. Taking a weighted average of decayed scores

        relevance is computed from keyword overlap between news text
        and the symbol's keyword list.
        """
        symbol_keys = SYMBOL_KEYWORDS.get(symbol, [])
        macro_keys  = MACRO_KEYWORDS

        weighted_scores = []
        total_weight = 0.0

        for item in news:
            text_lower = (item.title + " " + item.description).lower()

            # Compute relevance weight
            symbol_hits = sum(1 for k in symbol_keys if k.lower() in text_lower)
            macro_hits  = sum(1 for k in macro_keys  if k.lower() in text_lower)

            relevance = min((symbol_hits * 2 + macro_hits) / 5.0, 1.0)

            if relevance == 0:
                continue    # Skip irrelevant news

            # High-impact events get extra weight
            impact_multiplier = 2.0 if item.is_high_impact else 1.0
            weight = relevance * impact_multiplier

            weighted_scores.append(item.decayed_score * weight)
            total_weight += weight

        if total_weight == 0:
            return 0.0

        aggregated = sum(weighted_scores) / total_weight

        # Clamp to [-1, +1]
        return float(np.clip(aggregated, -1.0, 1.0))

    def get_high_impact_events(self) -> List[NewsItem]:
        """Returns only high-impact news items from the last 6 hours."""
        news = self.collector.fetch_news(max_age_hours=6)
        scored = [self._score_item(item) for item in news]
        return [item for item in scored if item.is_high_impact]

    def should_avoid_trading(self, symbol: str) -> Tuple[bool, str]:
        """
        Returns (True, reason) if macro conditions suggest avoiding
        new trades (e.g. during very high-impact news release window).
        """
        events = self.get_high_impact_events()
        if not events:
            return False, ""

        # If a high-impact event is very recent (< 30 min) and very negative,
        # recommend avoiding trading due to high uncertainty
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        very_recent = [e for e in events if e.published_at > recent_cutoff]

        if len(very_recent) >= 2:
            return True, f"High-impact news cluster detected: {very_recent[0].title[:50]}..."

        return False, ""
