"""Configuration management for karb."""

from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Wallet Configuration
    private_key: Optional[SecretStr] = Field(
        default=None,
        description="Private key for signing transactions (hex string with 0x prefix)",
    )
    wallet_address: Optional[str] = Field(
        default=None,
        description="Wallet address for trading",
    )

    # Network Configuration
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com",
        description="Polygon RPC endpoint URL",
    )
    chain_id: int = Field(
        default=137,
        description="Chain ID (137 for Polygon mainnet)",
    )

    # Trading Parameters
    min_profit_threshold: float = Field(
        default=0.005,
        description="Minimum profit threshold (0.005 = 0.5%)",
        ge=0.0,
        le=0.1,
    )
    max_position_size: float = Field(
        default=100.0,
        description="Maximum position size in USD per market",
        ge=1.0,
    )
    poll_interval_seconds: float = Field(
        default=2.0,
        description="Seconds between market polls",
        ge=0.5,
        le=60.0,
    )
    min_liquidity_usd: float = Field(
        default=1000.0,
        description="Minimum liquidity in USD to consider a market",
        ge=0.0,
    )

    # API Endpoints
    clob_base_url: str = Field(
        default="https://clob.polymarket.com",
        description="Polymarket CLOB API base URL",
    )
    gamma_base_url: str = Field(
        default="https://gamma-api.polymarket.com",
        description="Polymarket Gamma API base URL",
    )

    # Kalshi Configuration
    kalshi_api_key: Optional[str] = Field(
        default=None,
        description="Kalshi API key ID",
    )
    kalshi_private_key: Optional[SecretStr] = Field(
        default=None,
        description="Kalshi RSA private key (PEM format)",
    )
    kalshi_base_url: str = Field(
        default="https://api.elections.kalshi.com/trade-api/v2",
        description="Kalshi API base URL",
    )

    # Alerts (optional)
    telegram_bot_token: Optional[str] = Field(
        default=None,
        description="Telegram bot token for alerts",
    )
    telegram_chat_id: Optional[str] = Field(
        default=None,
        description="Telegram chat ID for alerts",
    )

    # Mode
    dry_run: bool = Field(
        default=True,
        description="If true, simulate trades without executing",
    )

    # Dashboard
    dashboard_username: str = Field(
        default="admin",
        description="Dashboard login username",
    )
    dashboard_password: str = Field(
        default="",
        description="Dashboard login password",
    )
    dashboard_port: int = Field(
        default=8080,
        description="Dashboard web server port",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )

    @field_validator("wallet_address", mode="before")
    @classmethod
    def validate_wallet_address(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError("Wallet address must be a valid Ethereum address (0x + 40 hex chars)")
        return v.lower()

    @field_validator("private_key", mode="before")
    @classmethod
    def validate_private_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not v.startswith("0x"):
            raise ValueError("Private key must start with 0x")
        if len(v) != 66:  # 0x + 64 hex chars
            raise ValueError("Private key must be 32 bytes (64 hex chars + 0x prefix)")
        return v

    def is_trading_enabled(self) -> bool:
        """Check if Polymarket trading credentials are configured."""
        return self.private_key is not None and self.wallet_address is not None

    def is_kalshi_enabled(self) -> bool:
        """Check if Kalshi credentials are configured."""
        return self.kalshi_api_key is not None and self.kalshi_private_key is not None


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from environment."""
    global _settings
    _settings = Settings()
    return _settings
