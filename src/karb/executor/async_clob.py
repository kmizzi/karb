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
        """Convert to API payload format."""
        return {
            "salt": str(self.salt),
            "maker": self.maker,
            "signer": self.signer,
            "taker": self.taker,
            "tokenId": self.token_id,
            "makerAmount": str(self.maker_amount),
            "takerAmount": str(self.taker_amount),
            "expiration": str(self.expiration),
            "nonce": str(self.nonce),
            "feeRateBps": str(self.fee_rate_bps),
            "side": self.side,
            "signatureType": self.signature_type,
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
        side_int = 0 if side == "BUY" else 1

        if side == "BUY":
            # Buying tokens: maker_amount = USDC, taker_amount = tokens
            taker_amount = int(size * 1e6)  # tokens (6 decimals)
            maker_amount = int(size * price * 1e6)  # USDC
        else:
            # Selling tokens: maker_amount = tokens, taker_amount = USDC
            maker_amount = int(size * 1e6)  # tokens
            taker_amount = int(size * price * 1e6)  # USDC

        # Generate unique salt
        salt = int(time.time() * 1000000) + int.from_bytes(
            hashlib.sha256(f"{token_id}{side}{price}{size}".encode()).digest()[:8],
            "big"
        )

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
            signature=signed.signature.hex(),
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
        neg_risk: bool = False,
    ) -> dict[str, Any]:
        """
        Sign and submit an order in one call.

        For maximum parallelization, use sign_order + post_order separately.
        """
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

        return await self.post_order(signed_order)

    async def submit_orders_parallel(
        self,
        orders: list[tuple[str, str, float, float, bool]],
    ) -> list[dict[str, Any]]:
        """
        Submit multiple orders in parallel.

        Args:
            orders: List of (token_id, side, price, size, neg_risk) tuples

        Returns:
            List of API responses
        """
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
            for token_id, side, price, size, neg_risk in orders
        ]
        signed_orders = await asyncio.gather(*sign_tasks)

        # Submit all orders in parallel
        post_tasks = [
            self.post_order(signed_order)
            for signed_order in signed_orders
        ]

        return await asyncio.gather(*post_tasks, return_exceptions=True)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an order by ID."""
        path = "/order"
        body = {"orderID": order_id}
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._get_l2_headers("DELETE", path, body_str)

        response = await self._client.delete(
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
