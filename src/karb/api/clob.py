"""CLOB API client for orderbook and order management."""

from decimal import Decimal
from typing import Any, Optional

import aiohttp

from karb.api.models import OrderBook, OrderBookLevel
from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)


class ClobClient:
    """Client for Polymarket CLOB API (orderbook and orders)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or get_settings().clob_base_url).rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Make a GET request to the API."""
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        async with session.get(url, params=params) as response:
            response.raise_for_status()
            return await response.json()

    async def _post(
        self,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        """Make a POST request to the API."""
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)

        try:
            async with session.post(url, json=data, headers=request_headers) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            log.error("CLOB API POST failed", url=url, error=str(e))
            raise

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """
        Fetch the orderbook for a token.

        Args:
            token_id: The outcome token ID

        Returns:
            OrderBook with bids and asks
        """
        params = {"token_id": token_id}
        try:
            data = await self._get("/book", params)
        except aiohttp.ClientResponseError as e:
            # 404 is common for markets without active orderbooks
            if e.status in (404, 502, 503):
                return OrderBook(token_id=token_id, bids=[], asks=[])
            raise

        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []

        # Parse bids
        for bid in data.get("bids", []):
            try:
                bids.append(
                    OrderBookLevel(
                        price=Decimal(str(bid.get("price", "0"))),
                        size=Decimal(str(bid.get("size", "0"))),
                    )
                )
            except (ValueError, TypeError):
                continue

        # Parse asks
        for ask in data.get("asks", []):
            try:
                asks.append(
                    OrderBookLevel(
                        price=Decimal(str(ask.get("price", "0"))),
                        size=Decimal(str(ask.get("size", "0"))),
                    )
                )
            except (ValueError, TypeError):
                continue

        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def get_price(self, token_id: str) -> Optional[Decimal]:
        """
        Get the current mid price for a token.

        Args:
            token_id: The outcome token ID

        Returns:
            Mid price or None
        """
        try:
            params = {"token_id": token_id}
            data = await self._get("/price", params)
            price = data.get("price") or data.get("mid")
            if price is not None:
                return Decimal(str(price))
            return None
        except Exception as e:
            log.warning("Failed to get price", token_id=token_id, error=str(e))
            return None

    async def get_prices(self, token_ids: list[str]) -> dict[str, Decimal]:
        """
        Get prices for multiple tokens.

        Args:
            token_ids: List of token IDs

        Returns:
            Dictionary mapping token_id to price
        """
        prices: dict[str, Decimal] = {}

        # Some CLOB APIs support batch price queries
        try:
            params = {"token_ids": ",".join(token_ids)}
            data = await self._get("/prices", params)

            if isinstance(data, dict):
                for token_id, price in data.items():
                    if price is not None:
                        prices[token_id] = Decimal(str(price))
            return prices
        except aiohttp.ClientResponseError as e:
            if e.status != 404:
                log.debug("Batch price endpoint not available, falling back to individual")
            pass

        # Fallback to individual queries
        for token_id in token_ids:
            price = await self.get_price(token_id)
            if price is not None:
                prices[token_id] = price

        return prices

    async def get_market_info(self, condition_id: str) -> Optional[dict[str, Any]]:
        """
        Get market info from CLOB API.

        Args:
            condition_id: Market condition ID

        Returns:
            Market info dictionary or None
        """
        try:
            params = {"condition_id": condition_id}
            return await self._get("/markets", params)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None
            raise

    async def get_midpoint(self, token_id: str) -> Optional[Decimal]:
        """
        Get midpoint price from orderbook.

        Args:
            token_id: The outcome token ID

        Returns:
            Midpoint price or None
        """
        orderbook = await self.get_orderbook(token_id)
        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        elif best_bid is not None:
            return best_bid
        elif best_ask is not None:
            return best_ask
        return None

    async def get_spread(self, token_id: str) -> Optional[Decimal]:
        """
        Get bid-ask spread for a token.

        Args:
            token_id: The outcome token ID

        Returns:
            Spread (ask - bid) or None
        """
        orderbook = await self.get_orderbook(token_id)
        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        if best_bid is not None and best_ask is not None:
            return best_ask - best_bid
        return None

    async def check_health(self) -> bool:
        """Check if CLOB API is healthy."""
        try:
            # Try a simple endpoint
            await self._get("/")
            return True
        except Exception:
            try:
                # Alternative health check
                await self._get("/time")
                return True
            except Exception:
                return False

    async def __aenter__(self) -> "ClobClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()
