"""
Async CLOB client for low-latency order execution.

Replaces synchronous py_clob_client with native async implementation
using httpx for HTTP and optimized signing.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)

# Polymarket contract addresses (Polygon mainnet)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# EIP-712 domain for order signing
ORDER_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,
}

ORDER_TYPES = {
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ]
}


@dataclass
class SignedOrder:
    """A signed order ready for submission."""
    salt: int
    maker: str
    signer: str
    taker: str
    token_id: str
    maker_amount: int
    taker_amount: int
    expiration: int
    nonce: int
    fee_rate_bps: int
    side: int  # 0 = BUY, 1 = SELL
    signature_type: int
    signature: str

    def to_dict(self) -> dict:
        """Convert to API payload format matching py_clob_client exactly."""
        # Side must be string "BUY" or "SELL", not integer
        side_str = "BUY" if self.side == 0 else "SELL"
        return {
            "salt": self.salt,  # Keep as integer
            "maker": self.maker,
            "signer": self.signer,
            "taker": self.taker,
            "tokenId": self.token_id,  # String
            "makerAmount": str(self.maker_amount),
            "takerAmount": str(self.taker_amount),
            "expiration": str(self.expiration),
            "nonce": str(self.nonce),
            "feeRateBps": str(self.fee_rate_bps),
            "side": side_str,
            "signatureType": self.signature_type,  # Keep as integer
            "signature": self.signature,
        }


class AsyncClobClient:
    """
    Async CLOB client for low-latency order execution.

    Key optimizations:
    - Native async HTTP with httpx
    - Connection pooling
    - Parallelized order submission
    """

    def __init__(
        self,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        host: str = "https://clob.polymarket.com",
        proxy_url: Optional[str] = None,
    ):
        self.private_key = private_key
        self.account = Account.from_key(private_key)
        self.address = self.account.address

        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase

        self.host = host.rstrip("/")
        self.proxy_url = proxy_url

        # Async HTTP client with connection pooling
        transport = None
        if proxy_url:
            transport = httpx.AsyncHTTPTransport(proxy=proxy_url)

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
            http2=True,  # Use HTTP/2 for better performance
        )

        # Cache for tick sizes and neg_risk status
        self._tick_sizes: dict[str, str] = {}
        self._neg_risk: dict[str, bool] = {}

        log.info("AsyncClobClient initialized", address=self.address)

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()

    async def get_neg_risk(self, token_id: str) -> bool:
        """
        Check if a token is neg_risk (uses NEG_RISK_CTF_EXCHANGE).

        Results are cached to avoid repeated API calls.
        """
        if token_id in self._neg_risk:
            return self._neg_risk[token_id]

        try:
            response = await self._client.get(
                f"{self.host}/neg-risk",
                params={"token_id": token_id},
            )
            if response.status_code == 200:
                data = response.json()
                is_neg_risk = data.get("neg_risk", False)
                self._neg_risk[token_id] = is_neg_risk
                return is_neg_risk
        except Exception as e:
            log.warning("Failed to check neg_risk", token_id=token_id, error=str(e))

        # Default to False if API call fails
        self._neg_risk[token_id] = False
        return False

    async def prefetch_neg_risk(self, token_ids: list[str], batch_size: int = 50) -> dict[str, bool]:
        """
        Pre-fetch neg_risk status for multiple tokens in parallel.

        This should be called at startup to warm the cache and avoid
        latency during order execution.

        Args:
            token_ids: List of token IDs to check
            batch_size: Number of concurrent requests (avoid overwhelming API)

        Returns:
            Dictionary of token_id -> neg_risk status
        """
        # Filter out tokens we already have cached
        tokens_to_fetch = [t for t in token_ids if t not in self._neg_risk]

        if not tokens_to_fetch:
            log.info("All neg_risk values already cached", cached=len(self._neg_risk))
            return self._neg_risk.copy()

        log.info(
            "Pre-fetching neg_risk status",
            total_tokens=len(token_ids),
            to_fetch=len(tokens_to_fetch),
            already_cached=len(token_ids) - len(tokens_to_fetch),
        )

        t0 = time.time()

        # Fetch in batches to avoid overwhelming the API
        for i in range(0, len(tokens_to_fetch), batch_size):
            batch = tokens_to_fetch[i:i + batch_size]
            tasks = [self.get_neg_risk(token_id) for token_id in batch]
            await asyncio.gather(*tasks, return_exceptions=True)

        t1 = time.time()

        # Count results
        neg_risk_count = sum(1 for v in self._neg_risk.values() if v)

        log.info(
            "neg_risk pre-fetch complete",
            fetched=len(tokens_to_fetch),
            neg_risk_true=neg_risk_count,
            neg_risk_false=len(self._neg_risk) - neg_risk_count,
            duration_ms=int((t1 - t0) * 1000),
        )

        return self._neg_risk.copy()

    def _build_hmac_signature(
        self,
        timestamp: int,
        method: str,
        request_path: str,
        body: Optional[str] = None,
    ) -> str:
        """Build HMAC signature for L2 authentication."""
        secret_bytes = base64.urlsafe_b64decode(self.api_secret)
        message = f"{timestamp}{method}{request_path}"
        if body:
            message += body

        h = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256)
        return base64.urlsafe_b64encode(h.digest()).decode("utf-8")

    def _get_l2_headers(self, method: str, path: str, body: Optional[str] = None) -> dict:
        """Generate L2 authentication headers."""
        timestamp = int(time.time())
        signature = self._build_hmac_signature(timestamp, method, path, body)

        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

    def sign_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        neg_risk: bool = False,
        fee_rate_bps: int = 0,
    ) -> SignedOrder:
        """
        Sign an order using EIP-712.

        This is the CPU-intensive part - consider running in executor for parallelization.
        """
        # Calculate amounts (6 decimals for USDC)
        # IMPORTANT: Polymarket requires specific precision:
        # - maker_amount: max 2 decimal places (divisible by 10000 after 1e6 scaling)
        # - taker_amount: max 4 decimal places (divisible by 100 after 1e6 scaling)
        # Use round() before int() to avoid floating-point precision errors
        side_int = 0 if side == "BUY" else 1

        if side == "BUY":
            # Buying tokens: maker_amount = USDC, taker_amount = tokens
            # Round USDC to 2 decimals, tokens to 4 decimals
            taker_amount_raw = round(size * 1e6)
            taker_amount = (taker_amount_raw // 100) * 100  # Ensure 4 decimal precision
            maker_amount_raw = round(size * price * 1e6)
            maker_amount = (maker_amount_raw // 10000) * 10000  # Ensure 2 decimal precision
        else:
            # Selling tokens: maker_amount = tokens, taker_amount = USDC
            maker_amount_raw = round(size * 1e6)
            maker_amount = (maker_amount_raw // 100) * 100  # Ensure 4 decimal precision
            taker_amount_raw = round(size * price * 1e6)
            taker_amount = (taker_amount_raw // 10000) * 10000  # Ensure 2 decimal precision

        # Generate unique salt (matching py_clob_client's approach)
        import random
        salt = round(time.time() * random.random())

        # Select exchange based on neg_risk
        exchange = NEG_RISK_CTF_EXCHANGE if neg_risk else CTF_EXCHANGE

        # Build order data
        order_data = {
            "salt": salt,
            "maker": self.address,
            "signer": self.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": 0,  # No expiration
            "nonce": 0,
            "feeRateBps": fee_rate_bps,
            "side": side_int,
            "signatureType": 0,  # EOA
        }

        # Create EIP-712 domain with exchange address
        domain = {
            **ORDER_DOMAIN,
            "verifyingContract": exchange,
        }

        # Sign the order
        signable = encode_typed_data(domain, ORDER_TYPES, order_data)
        signed = self.account.sign_message(signable)

        return SignedOrder(
            salt=salt,
            maker=self.address,
            signer=self.address,
            taker="0x0000000000000000000000000000000000000000",
            token_id=token_id,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            expiration=0,
            nonce=0,
            fee_rate_bps=fee_rate_bps,
            side=side_int,
            signature_type=0,
            signature="0x" + signed.signature.hex(),  # Must include 0x prefix
        )

    async def post_order(
        self,
        signed_order: SignedOrder,
        order_type: str = "GTC",
    ) -> dict[str, Any]:
        """
        Submit a signed order to the CLOB API.

        This is the network-bound part - fully async.
        """
        path = "/order"
        body = {
            "order": signed_order.to_dict(),
            "owner": self.api_key,
            "orderType": order_type,
        }

        # Serialize with exact formatting for signature
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        headers = self._get_l2_headers("POST", path, body_str)

        response = await self._client.post(
            f"{self.host}{path}",
            headers=headers,
            content=body_str,
        )

        if response.status_code != 200:
            error_text = response.text
            log.error("Order submission failed", status=response.status_code, error=error_text)
            raise Exception(f"Order failed: {response.status_code} - {error_text}")

        return response.json()

    async def submit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        neg_risk: Optional[bool] = None,
        order_type: str = "FOK",  # Default to Fill-or-Kill for arbitrage
    ) -> dict[str, Any]:
        """
        Sign and submit an order in one call.

        Args:
            neg_risk: If None, auto-detects from API. If provided, uses that value.
            order_type: "FOK" (Fill-or-Kill), "GTC" (Good-til-Cancelled), "GTD", "FAK"

        For maximum parallelization, use sign_order + post_order separately.
        """
        t0 = time.time()

        # Auto-detect neg_risk if not provided
        if neg_risk is None:
            neg_risk = await self.get_neg_risk(token_id)
        t1 = time.time()

        # Run signing in thread pool to not block event loop
        loop = asyncio.get_event_loop()
        signed_order = await loop.run_in_executor(
            None,
            self.sign_order,
            token_id,
            side,
            price,
            size,
            neg_risk,
        )
        t2 = time.time()

        result = await self.post_order(signed_order, order_type=order_type)
        t3 = time.time()

        log.debug(
            "Order timing breakdown",
            neg_risk_ms=int((t1 - t0) * 1000),
            sign_ms=int((t2 - t1) * 1000),
            submit_ms=int((t3 - t2) * 1000),
            total_ms=int((t3 - t0) * 1000),
        )

        return result

    async def submit_orders_parallel(
        self,
        orders: list[tuple[str, str, float, float, Optional[bool]]],
        order_type: str = "FOK",
    ) -> list[dict[str, Any]]:
        """
        Submit multiple orders in parallel with optimized latency.

        Args:
            orders: List of (token_id, side, price, size, neg_risk) tuples
                    neg_risk can be None for auto-detection
            order_type: Order type - "FOK" (Fill-or-Kill) recommended for arbitrage

        Returns:
            List of API responses
        """
        t0 = time.time()

        # Auto-detect neg_risk for orders where it's None, in parallel
        tokens_needing_check = [
            token_id for token_id, side, price, size, neg_risk in orders if neg_risk is None
        ]

        # Fetch all neg_risk values in parallel (only for tokens that need checking)
        if tokens_needing_check:
            neg_risk_results = await asyncio.gather(
                *[self.get_neg_risk(token_id) for token_id in tokens_needing_check]
            )
            neg_risk_map = dict(zip(tokens_needing_check, neg_risk_results))
        else:
            neg_risk_map = {}

        # Build resolved orders with neg_risk values
        resolved_orders = []
        for token_id, side, price, size, neg_risk in orders:
            if neg_risk is None:
                neg_risk = neg_risk_map.get(token_id, False)
            resolved_orders.append((token_id, side, price, size, neg_risk))
        t1 = time.time()

        # Sign all orders in parallel using thread pool
        loop = asyncio.get_event_loop()
        sign_tasks = [
            loop.run_in_executor(
                None,
                self.sign_order,
                token_id,
                side,
                price,
                size,
                neg_risk,
            )
            for token_id, side, price, size, neg_risk in resolved_orders
        ]
        signed_orders = await asyncio.gather(*sign_tasks)
        t2 = time.time()

        # Submit all orders in parallel
        post_tasks = [
            self.post_order(signed_order, order_type=order_type)
            for signed_order in signed_orders
        ]

        results = await asyncio.gather(*post_tasks, return_exceptions=True)
        t3 = time.time()

        log.debug(
            "Parallel order timing breakdown",
            neg_risk_ms=int((t1 - t0) * 1000),
            sign_ms=int((t2 - t1) * 1000),
            submit_ms=int((t3 - t2) * 1000),
            total_ms=int((t3 - t0) * 1000),
            order_count=len(orders),
        )

        return results

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an order by ID."""
        path = "/order"
        body = {"orderID": order_id}
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._get_l2_headers("DELETE", path, body_str)

        response = await self._client.request(
            "DELETE",
            f"{self.host}{path}",
            headers=headers,
            content=body_str,
        )

        return response.json()

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Get order details by ID."""
        path = f"/order/{order_id}"
        headers = self._get_l2_headers("GET", path)

        response = await self._client.get(
            f"{self.host}{path}",
            headers=headers,
        )

        return response.json()

    async def cancel_all(self) -> dict[str, Any]:
        """Cancel all open orders."""
        path = "/orders"
        headers = self._get_l2_headers("DELETE", path)

        response = await self._client.delete(
            f"{self.host}{path}",
            headers=headers,
        )

        return response.json()

    async def get_orders(self) -> list[dict]:
        """Get open orders."""
        path = "/orders"
        headers = self._get_l2_headers("GET", path)

        response = await self._client.get(
            f"{self.host}{path}",
            headers=headers,
        )

        return response.json()

    async def get_trades(self) -> list[dict]:
        """Get trade history."""
        path = "/trades"
        headers = self._get_l2_headers("GET", path)

        response = await self._client.get(
            f"{self.host}{path}",
            headers=headers,
        )

        return response.json()

    async def get_positions(self) -> list[dict]:
        """Get current positions from the Data API."""
        # Positions are on the Data API, not the CLOB API
        data_api_url = "https://data-api.polymarket.com"
        path = f"/positions?user={self.address}"

        try:
            response = await self._client.get(f"{data_api_url}{path}")

            if response.status_code != 200:
                log.error("Failed to get positions", status=response.status_code, body=response.text[:200])
                return []

            return response.json()
        except Exception as e:
            log.error("Error fetching positions", error=str(e))
            return []


async def create_async_clob_client() -> Optional[AsyncClobClient]:
    """Create an AsyncClobClient from settings."""
    settings = get_settings()

    if not settings.poly_api_key or not settings.private_key:
        log.warning("Missing API credentials for async CLOB client")
        return None

    # Build proxy URL if configured
    proxy_url = None
    if settings.socks5_proxy_host and settings.socks5_proxy_port:
        proxy_url = (
            f"socks5://{settings.socks5_proxy_user}:{settings.socks5_proxy_pass.get_secret_value()}"
            f"@{settings.socks5_proxy_host}:{settings.socks5_proxy_port}"
        )

    return AsyncClobClient(
        private_key=settings.private_key.get_secret_value(),
        api_key=settings.poly_api_key,
        api_secret=settings.poly_api_secret.get_secret_value(),
        api_passphrase=settings.poly_api_passphrase.get_secret_value(),
        proxy_url=proxy_url,
    )
