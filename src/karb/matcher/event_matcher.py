"""Event matcher for cross-platform arbitrage."""

import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Optional

from karb.api.kalshi import KalshiMarket
from karb.api.models import Market as PolyMarket
from karb.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class MatchedEvent:
    """A matched event across Polymarket and Kalshi."""
    polymarket: PolyMarket
    kalshi: KalshiMarket
    confidence: float  # 0.0 to 1.0
    match_type: str  # "exact", "fuzzy", "manual"

    @property
    def poly_yes_ask(self) -> Optional[Decimal]:
        """Polymarket YES ask price."""
        return self.polymarket.yes_price

    @property
    def kalshi_yes_ask(self) -> Optional[Decimal]:
        """Kalshi YES ask price."""
        return self.kalshi.yes_ask

    @property
    def price_diff(self) -> Optional[Decimal]:
        """Price difference (Kalshi - Polymarket)."""
        if self.poly_yes_ask and self.kalshi_yes_ask:
            return self.kalshi_yes_ask - self.poly_yes_ask
        return None

    @property
    def abs_price_diff(self) -> Optional[Decimal]:
        """Absolute price difference."""
        diff = self.price_diff
        return abs(diff) if diff else None


# Manual mappings for known equivalent events
# Format: (polymarket_pattern, kalshi_ticker_pattern)
MANUAL_MAPPINGS = [
    # Bitcoin price markets
    (r"bitcoin.*\$?(\d+)k", r"BTC.*{0}000"),
    # Presidential elections
    (r"trump.*win.*2024", r"PRES.*TRUMP"),
    (r"harris.*win.*2024", r"PRES.*HARRIS"),
    (r"biden.*win.*2024", r"PRES.*BIDEN"),
    # Fed rate decisions
    (r"fed.*rate.*cut", r"FED.*CUT"),
    (r"fed.*rate.*hike", r"FED.*HIKE"),
]


def normalize_text(text: str) -> str:
    """Normalize text for comparison."""
    # Lowercase
    text = text.lower()
    # Remove punctuation except numbers
    text = re.sub(r'[^\w\s\d]', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text."""
    text = normalize_text(text)

    # Common stop words to ignore
    stop_words = {
        'the', 'a', 'an', 'will', 'be', 'is', 'are', 'was', 'were',
        'to', 'of', 'in', 'on', 'at', 'by', 'for', 'with', 'as',
        'or', 'and', 'but', 'if', 'than', 'that', 'this', 'it',
        'yes', 'no', 'before', 'after', 'during',
    }

    words = set(text.split())
    return words - stop_words


def similarity_score(text1: str, text2: str) -> float:
    """Calculate similarity between two strings."""
    norm1 = normalize_text(text1)
    norm2 = normalize_text(text2)

    # Use SequenceMatcher for fuzzy matching
    return SequenceMatcher(None, norm1, norm2).ratio()


def keyword_overlap_score(text1: str, text2: str) -> float:
    """Calculate keyword overlap score."""
    kw1 = extract_keywords(text1)
    kw2 = extract_keywords(text2)

    if not kw1 or not kw2:
        return 0.0

    overlap = kw1 & kw2
    union = kw1 | kw2

    return len(overlap) / len(union) if union else 0.0


class EventMatcher:
    """
    Matches events between Polymarket and Kalshi.

    Uses multiple strategies:
    1. Manual mappings for known equivalent events
    2. Keyword-based matching
    3. Fuzzy string matching
    """

    def __init__(
        self,
        min_confidence: float = 0.6,
        use_manual_mappings: bool = True,
    ) -> None:
        self.min_confidence = min_confidence
        self.use_manual_mappings = use_manual_mappings

        # Cache of matched events
        self._cache: dict[str, Optional[str]] = {}

    def match(
        self,
        poly_market: PolyMarket,
        kalshi_markets: list[KalshiMarket],
    ) -> Optional[MatchedEvent]:
        """
        Find the best matching Kalshi market for a Polymarket event.

        Args:
            poly_market: Polymarket market to match
            kalshi_markets: List of Kalshi markets to search

        Returns:
            MatchedEvent if found, None otherwise
        """
        poly_text = poly_market.question

        best_match: Optional[KalshiMarket] = None
        best_confidence = 0.0
        best_type = "none"

        for kalshi in kalshi_markets:
            kalshi_text = f"{kalshi.title} {kalshi.subtitle}"

            # Try manual mapping first
            if self.use_manual_mappings:
                manual_conf = self._check_manual_mapping(poly_text, kalshi.ticker)
                if manual_conf > best_confidence:
                    best_match = kalshi
                    best_confidence = manual_conf
                    best_type = "manual"
                    continue

            # Calculate combined score
            sim_score = similarity_score(poly_text, kalshi_text)
            kw_score = keyword_overlap_score(poly_text, kalshi_text)

            # Weighted combination
            confidence = (sim_score * 0.4) + (kw_score * 0.6)

            if confidence > best_confidence:
                best_match = kalshi
                best_confidence = confidence
                best_type = "fuzzy"

        if best_match and best_confidence >= self.min_confidence:
            return MatchedEvent(
                polymarket=poly_market,
                kalshi=best_match,
                confidence=best_confidence,
                match_type=best_type,
            )

        return None

    def _check_manual_mapping(self, poly_text: str, kalshi_ticker: str) -> float:
        """Check if there's a manual mapping match."""
        poly_norm = normalize_text(poly_text)
        kalshi_norm = kalshi_ticker.lower()

        for poly_pattern, kalshi_pattern in MANUAL_MAPPINGS:
            poly_match = re.search(poly_pattern, poly_norm, re.IGNORECASE)
            if poly_match:
                # Build the kalshi pattern with captured groups
                try:
                    built_pattern = kalshi_pattern.format(*poly_match.groups())
                except (IndexError, KeyError):
                    built_pattern = kalshi_pattern

                if re.search(built_pattern, kalshi_norm, re.IGNORECASE):
                    return 0.95  # High confidence for manual matches

        return 0.0

    def match_batch(
        self,
        poly_markets: list[PolyMarket],
        kalshi_markets: list[KalshiMarket],
    ) -> list[MatchedEvent]:
        """Match multiple Polymarket events to Kalshi."""
        matches = []

        for poly in poly_markets:
            match = self.match(poly, kalshi_markets)
            if match:
                matches.append(match)
                log.debug(
                    "Matched event",
                    poly=poly.question[:50],
                    kalshi=match.kalshi.ticker,
                    confidence=f"{match.confidence:.2f}",
                )

        log.info(
            "Event matching complete",
            poly_count=len(poly_markets),
            kalshi_count=len(kalshi_markets),
            matches=len(matches),
        )

        return matches

    def find_arbitrage(
        self,
        matches: list[MatchedEvent],
        min_spread: Decimal = Decimal("0.02"),
    ) -> list[MatchedEvent]:
        """
        Find arbitrage opportunities from matched events.

        Args:
            matches: List of matched events
            min_spread: Minimum price spread to consider (default 2%)

        Returns:
            List of matches with arbitrage opportunities
        """
        opportunities = []

        for match in matches:
            diff = match.abs_price_diff
            if diff and diff >= min_spread:
                opportunities.append(match)
                log.info(
                    "Cross-platform arbitrage found",
                    poly=match.polymarket.question[:40],
                    kalshi=match.kalshi.ticker,
                    poly_price=f"${float(match.poly_yes_ask or 0):.2f}",
                    kalshi_price=f"${float(match.kalshi_yes_ask or 0):.2f}",
                    spread=f"{float(diff) * 100:.1f}%",
                )

        return opportunities
