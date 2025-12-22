# karb

Polymarket arbitrage bot - automated detection and execution of risk-free arbitrage opportunities on prediction markets.

## Strategy

Pure arbitrage: when YES + NO token prices sum to less than $1.00, buy both. One token always pays out $1.00, guaranteeing profit regardless of outcome.

```
Example:
YES @ $0.48 + NO @ $0.49 = $0.97 cost
Payout = $1.00 (guaranteed)
Profit = $0.03 per dollar (3.09%)
```

## Status

Live trading enabled. Dashboard available at configured domain.

## Features

- Real-time WebSocket price monitoring (6 parallel connections, up to 1500 markets)
- Automatic arbitrage detection and execution
- Order monitoring with 10-second timeout and auto-cancellation
- Market filtering by liquidity ($10k+ default) and resolution date (7 days default)
- Web dashboard with live order visibility (HTTPS with auto SSL)
- Slack notifications for trades
- SOCKS5 proxy support for geo-restricted order placement

## Setup

```bash
# Clone
git clone https://github.com/kmizzi/karb.git
cd karb

# Install dependencies
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your settings

# Generate Polymarket API credentials
python -c "
from py_clob_client.client import ClobClient
import os
client = ClobClient('https://clob.polymarket.com', key=os.environ['PRIVATE_KEY'], chain_id=137)
creds = client.create_or_derive_api_creds()
print(f'POLY_API_KEY={creds.api_key}')
print(f'POLY_API_SECRET={creds.api_secret}')
print(f'POLY_API_PASSPHRASE={creds.api_passphrase}')
"

# Approve Polymarket contracts (one-time setup)
python scripts/approve_usdc.py

# Run
karb run --live --realtime
```

## Configuration

Required environment variables:

```bash
# Wallet
PRIVATE_KEY=0x...                    # Your wallet private key
WALLET_ADDRESS=0x...                 # Your wallet address

# Polymarket L2 API Credentials (generate with script above)
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...

# Trading Parameters
MIN_PROFIT_THRESHOLD=0.005           # 0.5% minimum profit
MAX_POSITION_SIZE=100                # Max $100 per trade
MIN_LIQUIDITY_USD=10000              # $10k minimum market liquidity
MAX_DAYS_UNTIL_RESOLUTION=7          # Skip markets resolving later
NUM_WS_CONNECTIONS=6                 # WebSocket connections (250 markets each)
DRY_RUN=true                         # Set to false for live trading

# Dashboard (optional - omit for no auth)
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=...
```

See `.env.example` for all available options.

## Contract Approvals

Before trading, you must approve Polymarket's smart contracts to spend your USDC.e:

```bash
# Run the approval script (requires PRIVATE_KEY in environment)
python scripts/approve_usdc.py
```

This approves:
- CTF Exchange
- Neg Risk Exchange
- Conditional Tokens
- Neg Risk Adapter

## Geo-Restrictions

Polymarket blocks US IP addresses for order placement. The recommended architecture:

- **Bot server (us-east-1)**: Low-latency WebSocket connection for price monitoring
- **Proxy server (ca-central-1 Montreal)**: SOCKS5 proxy for order placement

Configure the proxy in your `.env`:
```bash
SOCKS5_PROXY_HOST=your-proxy-ip
SOCKS5_PROXY_PORT=1080
SOCKS5_PROXY_USER=karb
SOCKS5_PROXY_PASS=your-password
```

See `infra/` for OpenTofu + Ansible deployment scripts.

## Documentation

See [PRD.md](PRD.md) for full product requirements and technical architecture.

## License

MIT
