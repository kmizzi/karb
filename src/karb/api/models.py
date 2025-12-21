"""Data models for Polymarket API responses."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class Token:
    """Represents a YES or NO outcome token."""

    token_id: str
    outcome: str  # "Yes" or "No"
    price: Decimal = Decimal("0")


@dataclass
class OrderBookLevel:
    """A single price level in the order book."""

    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    """Order book for a single token."""

    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Get the best (highest) bid price."""
        if not self.bids:
            return None
        return max(level.price for level in self.bids)

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Get the best (lowest) ask price."""
        if not self.asks:
            return None
        return min(level.price for level in self.asks)

    @property
    def best_bid_size(self) -> Optional[Decimal]:
        """Get size at best bid."""
        if not self.bids:
            return None
        best_price = self.best_bid
        for level in self.bids:
            if level.price == best_price:
                return level.size
        return None

    @property
    def best_ask_size(self) -> Optional[Decimal]:
        """Get size at best ask."""
        if not self.asks:
            return None
        best_price = self.best_ask
        for level in self.asks:
            if level.price == best_price:
                return level.size
        return None


@dataclass
class Market:
    """A Polymarket prediction market."""

    id: str
    condition_id: str
    question: str
    slug: str

    yes_token: Token
    no_token: Token

    volume: Decimal = Decimal("0")
    liquidity: Decimal = Decimal("0")

    active: bool = True
    closed: bool = False

    end_date: Optional[datetime] = None

    # Order books (populated separately)
    yes_orderbook: Optional[OrderBook] = None
    no_orderbook: Optional[OrderBook] = None

    @property
    def yes_price(self) -> Decimal:
        """Get current YES price."""
        return self.yes_token.price

    @property
    def no_price(self) -> Decimal:
        """Get current NO price."""
        return self.no_token.price

    @property
    def combined_price(self) -> Decimal:
        """Get YES + NO combined price."""
        return self.yes_price + self.no_price

    @property
    def spread(self) -> Decimal:
        """Get arbitrage spread (1 - combined_price)."""
        return Decimal("1") - self.combined_price


@dataclass
class ArbitrageOpportunity:
    """An identified arbitrage opportunity."""

    market: Market
    yes_ask: Decimal  # Price to buy YES
    no_ask: Decimal   # Price to buy NO
    combined_cost: Decimal  # Total cost to buy both
    profit_pct: Decimal  # Expected profit percentage

    yes_size_available: Decimal  # Liquidity at YES ask
    no_size_available: Decimal   # Liquidity at NO ask
    max_trade_size: Decimal      # Max tradeable size (min of both)

    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def expected_profit_usd(self) -> Decimal:
        """Expected profit in USD for max trade size."""
        return self.max_trade_size * self.profit_pct


@dataclass
class Order:
    """An order to be placed on Polymarket."""

    token_id: str
    side: str  # "BUY" or "SELL"
    price: Decimal
    size: Decimal

    # Filled after placement
    order_id: Optional[str] = None
    status: str = "pending"  # pending, open, filled, cancelled, failed
    filled_size: Decimal = Decimal("0")


@dataclass
class Trade:
    """A completed trade."""

    market_id: str
    yes_order: Order
    no_order: Order

    total_cost: Decimal
    expected_profit: Decimal

    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Updated after resolution
    actual_profit: Optional[Decimal] = None
    resolved: bool = False
