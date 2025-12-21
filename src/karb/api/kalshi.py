"""Kalshi API client for cross-platform arbitrage."""

import asyncio
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class KalshiMarket:
    """Represents a Kalshi market."""
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    yes_bid: Optional[Decimal]
    yes_ask: Optional[Decimal]
    no_bid: Optional[Decimal]
    no_ask: Optional[Decimal]
    volume: int
    open_interest: int
    status: str
    close_time: Optional[str] = None

    @property
    def mid_price(self) -> Optional[Decimal]:
        """Calculate mid price."""
        if self.yes_bid and self.yes_ask:
            return (self.yes_bid + self.yes_ask) / 2
        return None


class KalshiClient:
    """
    Client for Kalshi API.

    Uses the official kalshi-python SDK for authentication and requests.
    """

    def __init__(self) -> None:
        settings = get_settings()

        self._api_key = settings.kalshi_api_key
        self._private_key = (
            settings.kalshi_private_key.get_secret_value()
            if settings.kalshi_private_key else None
        )
        self._base_url = settings.kalshi_base_url
        self._client = None
        self._config = None

    def _get_client(self):
        """Get or create the Kalshi client."""
        if self._client is None:
            if not self._api_key or not self._private_key:
                raise RuntimeError("Kalshi credentials not configured")

            from kalshi_python import Configuration, KalshiClient as SDK

            self._config = Configuration(host=self._base_url)
            self._config.api_key_id = self._api_key
            self._config.private_key_pem = self._private_key

            self._client = SDK(self._config)
            log.info("Kalshi client initialized")

        return self._client

    async def get_balance(self) -> Decimal:
        """Get account balance."""
        def _sync():
            client = self._get_client()
            response = client.get_balance()
            return Decimal(str(response.balance)) / 100  # Cents to dollars

        return await asyncio.to_thread(_sync)

    async def get_events(self, status: str = "open", limit: int = 100) -> list[dict]:
        """Get list of events."""
        def _sync():
            client = self._get_client()
            response = client.get_events(status=status, limit=limit)
            return [e.to_dict() for e in response.events]

        return await asyncio.to_thread(_sync)

    async def get_event(self, event_ticker: str) -> Optional[dict]:
        """Get a specific event by ticker."""
        def _sync():
            client = self._get_client()
            try:
                response = client.get_event(event_ticker)
                return response.event.to_dict()
            except Exception as e:
                log.debug("Event not found", ticker=event_ticker, error=str(e))
                return None

        return await asyncio.to_thread(_sync)

    async def get_markets(
        self,
        event_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[KalshiMarket]:
        """Get markets, optionally filtered by event."""
        def _sync():
            import requests
            from kalshi_python.api_client import KalshiAuth

            client = self._get_client()

            # Write private key to temp file for SDK auth
            with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:
                f.write(client.private_key_pem)
                key_path = f.name

            try:
                auth = KalshiAuth(client.api_key_id, key_path)

                params = {"limit": limit, "status": status}
                if event_ticker:
                    params["event_ticker"] = event_ticker

                url = f"{self._base_url}/markets"
                headers = auth.create_auth_headers("GET", "/markets")
                resp = requests.get(url, params=params, headers=headers)
                resp.raise_for_status()
                return resp.json().get("markets", [])
            finally:
                Path(key_path).unlink(missing_ok=True)

        raw_markets = await asyncio.to_thread(_sync)

        markets = []
        for m in raw_markets:
            try:
                market = KalshiMarket(
                    ticker=m.get("ticker", ""),
                    event_ticker=m.get("event_ticker", ""),
                    title=m.get("title") or "",
                    subtitle=m.get("subtitle") or "",
                    yes_bid=Decimal(str(m["yes_bid"])) / 100 if m.get("yes_bid") else None,
                    yes_ask=Decimal(str(m["yes_ask"])) / 100 if m.get("yes_ask") else None,
                    no_bid=Decimal(str(m["no_bid"])) / 100 if m.get("no_bid") else None,
                    no_ask=Decimal(str(m["no_ask"])) / 100 if m.get("no_ask") else None,
                    volume=m.get("volume") or 0,
                    open_interest=m.get("open_interest") or 0,
                    status=m.get("status") or "unknown",
                    close_time=m.get("close_time"),
                )
                markets.append(market)
            except Exception as e:
                log.debug("Failed to parse market", ticker=m.get('ticker', 'unknown'), error=str(e))

        return markets

    async def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """Get a specific market by ticker."""
        def _sync():
            client = self._get_client()
            try:
                response = client.get_market(ticker)
                return response.market
            except Exception as e:
                log.debug("Market not found", ticker=ticker, error=str(e))
                return None

        m = await asyncio.to_thread(_sync)

        if not m:
            return None

        return KalshiMarket(
            ticker=m.ticker,
            event_ticker=m.event_ticker,
            title=m.title or "",
            subtitle=m.subtitle or "",
            yes_bid=Decimal(str(m.yes_bid)) / 100 if m.yes_bid else None,
            yes_ask=Decimal(str(m.yes_ask)) / 100 if m.yes_ask else None,
            no_bid=Decimal(str(m.no_bid)) / 100 if m.no_bid else None,
            no_ask=Decimal(str(m.no_ask)) / 100 if m.no_ask else None,
            volume=m.volume or 0,
            open_interest=m.open_interest or 0,
            status=m.status or "unknown",
            close_time=m.close_time,
        )

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Get orderbook for a market."""
        def _sync():
            client = self._get_client()
            response = client.get_market_orderbook(ticker, depth=depth)
            return response.orderbook.to_dict()

        return await asyncio.to_thread(_sync)

    async def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,  # Number of contracts
        price: int,  # Price in cents (1-99)
        order_type: str = "limit",
    ) -> dict:
        """Place an order on Kalshi."""
        settings = get_settings()

        if settings.dry_run:
            log.info(
                "DRY RUN: Would place Kalshi order",
                ticker=ticker,
                side=side,
                action=action,
                count=count,
                price=price,
            )
            return {"dry_run": True}

        def _sync():
            client = self._get_client()
            response = client.create_order(
                ticker=ticker,
                side=side,
                action=action,
                count=count,
                type=order_type,
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
            )
            return response.order.to_dict()

        result = await asyncio.to_thread(_sync)
        log.info("Kalshi order placed", ticker=ticker, result=result)
        return result

    async def search_markets(self, query: str) -> list[KalshiMarket]:
        """Search markets by keyword."""
        # Kalshi doesn't have a direct search API, so we'll fetch and filter
        all_markets = await self.get_markets(limit=500)

        query_lower = query.lower()
        matching = [
            m for m in all_markets
            if query_lower in m.title.lower()
            or query_lower in m.subtitle.lower()
            or query_lower in m.ticker.lower()
        ]

        return matching

    async def check_health(self) -> bool:
        """Check if Kalshi API is accessible."""
        try:
            await self.get_balance()
            return True
        except Exception as e:
            log.error("Kalshi health check failed", error=str(e))
            return False

    async def close(self) -> None:
        """Close the client."""
        self._client = None
        self._config = None

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
