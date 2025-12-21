"""EIP-712 signing for Polymarket orders."""

import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)


# Polymarket contract addresses on Polygon
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
COLLATERAL_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon


class OrderSide(IntEnum):
    """Order side enum matching Polymarket contract."""

    BUY = 0
    SELL = 1


class SignatureType(IntEnum):
    """Signature type for orders."""

    EOA = 0
    POLY_PROXY = 1
    POLY_GNOSIS_SAFE = 2


@dataclass
class OrderData:
    """Raw order data for signing."""

    maker: str
    taker: str
    token_id: str
    maker_amount: int  # In base units (USDC has 6 decimals)
    taker_amount: int  # In base units (outcome tokens have 6 decimals)
    side: OrderSide
    fee_rate_bps: int = 0
    nonce: int = 0
    expiration: int = 0  # 0 = no expiration
    signature_type: SignatureType = SignatureType.EOA


@dataclass
class SignedOrder:
    """A signed order ready for submission."""

    order: OrderData
    signature: str
    salt: int


# EIP-712 domain for Polymarket
DOMAIN_DATA = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,  # Polygon mainnet
}


# EIP-712 type definitions
ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
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
    ],
}


class OrderSigner:
    """Signs orders using EIP-712 for Polymarket."""

    def __init__(
        self,
        private_key: Optional[str] = None,
        wallet_address: Optional[str] = None,
    ) -> None:
        settings = get_settings()

        self._private_key = private_key
        if self._private_key is None and settings.private_key:
            self._private_key = settings.private_key.get_secret_value()

        self._wallet_address = wallet_address or settings.wallet_address

        if self._private_key:
            # Derive address from private key if not provided
            account = Account.from_key(self._private_key)
            derived_address = account.address.lower()

            if self._wallet_address:
                if self._wallet_address.lower() != derived_address:
                    raise ValueError(
                        f"Wallet address {self._wallet_address} does not match "
                        f"private key address {derived_address}"
                    )
            else:
                self._wallet_address = derived_address

    @property
    def address(self) -> Optional[str]:
        """Get the wallet address."""
        return self._wallet_address

    @property
    def is_configured(self) -> bool:
        """Check if signer is properly configured."""
        return self._private_key is not None and self._wallet_address is not None

    def generate_salt(self) -> int:
        """Generate a random salt for order uniqueness."""
        return secrets.randbits(128)

    def create_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
        fee_rate_bps: int = 0,
    ) -> OrderData:
        """
        Create an order data structure.

        Args:
            token_id: The outcome token ID
            side: BUY or SELL
            price: Price per token (0-1)
            size: Number of tokens (in whole units)
            fee_rate_bps: Fee rate in basis points

        Returns:
            OrderData structure
        """
        if not self._wallet_address:
            raise ValueError("Wallet address not configured")

        # Convert to base units (6 decimals for both USDC and outcome tokens)
        size_base = int(size * Decimal("1000000"))

        if side == OrderSide.BUY:
            # Buying: maker pays USDC, receives tokens
            # maker_amount = price * size (in USDC base units)
            # taker_amount = size (in token base units)
            maker_amount = int(price * size * Decimal("1000000"))
            taker_amount = size_base
        else:
            # Selling: maker pays tokens, receives USDC
            maker_amount = size_base
            taker_amount = int(price * size * Decimal("1000000"))

        return OrderData(
            maker=self._wallet_address,
            taker="0x0000000000000000000000000000000000000000",  # Anyone can fill
            token_id=token_id,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            side=side,
            fee_rate_bps=fee_rate_bps,
            nonce=0,
            expiration=0,  # No expiration
            signature_type=SignatureType.EOA,
        )

    def sign_order(self, order: OrderData) -> SignedOrder:
        """
        Sign an order using EIP-712.

        Args:
            order: The order to sign

        Returns:
            SignedOrder with signature
        """
        if not self._private_key:
            raise ValueError("Private key not configured")
        if not self._wallet_address:
            raise ValueError("Wallet address not configured")

        salt = self.generate_salt()

        # Build the message for signing
        message = {
            "salt": salt,
            "maker": order.maker,
            "signer": self._wallet_address,
            "taker": order.taker,
            "tokenId": int(order.token_id),
            "makerAmount": order.maker_amount,
            "takerAmount": order.taker_amount,
            "expiration": order.expiration,
            "nonce": order.nonce,
            "feeRateBps": order.fee_rate_bps,
            "side": int(order.side),
            "signatureType": int(order.signature_type),
        }

        # Create typed data for signing
        typed_data = {
            "types": ORDER_TYPES,
            "primaryType": "Order",
            "domain": DOMAIN_DATA,
            "message": message,
        }

        # Sign the message
        signable = encode_typed_data(full_message=typed_data)
        signed = Account.sign_message(signable, self._private_key)

        return SignedOrder(
            order=order,
            signature=signed.signature.hex(),
            salt=salt,
        )

    def order_to_api_payload(self, signed_order: SignedOrder) -> dict[str, Any]:
        """
        Convert a signed order to API payload format.

        Args:
            signed_order: The signed order

        Returns:
            Dictionary ready for API submission
        """
        order = signed_order.order

        return {
            "order": {
                "salt": str(signed_order.salt),
                "maker": order.maker,
                "signer": self._wallet_address,
                "taker": order.taker,
                "tokenId": order.token_id,
                "makerAmount": str(order.maker_amount),
                "takerAmount": str(order.taker_amount),
                "expiration": str(order.expiration),
                "nonce": str(order.nonce),
                "feeRateBps": str(order.fee_rate_bps),
                "side": "BUY" if order.side == OrderSide.BUY else "SELL",
                "signatureType": order.signature_type,
            },
            "signature": f"0x{signed_order.signature}",
            "owner": self._wallet_address,
            "orderType": "GTC",  # Good-til-cancelled
        }
