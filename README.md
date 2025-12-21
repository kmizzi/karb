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

🚧 Under development

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

# Run
python -m karb
```

## Configuration

See `.env.example` for required environment variables.

## Documentation

See [PRD.md](PRD.md) for full product requirements and technical architecture.

## License

MIT
