"""FastAPI dashboard application."""

import asyncio
import json
import secrets
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from karb.config import get_settings
from karb.tracking.portfolio import PortfolioTracker
from karb.tracking.trades import TradeLog
from karb.utils.logging import get_logger

log = get_logger(__name__)

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_DIR.mkdir(exist_ok=True)

# Shared state files from scanner
STATS_FILE = Path.home() / ".karb" / "scanner_stats.json"
ALERTS_FILE = Path.home() / ".karb" / "scanner_alerts.json"
MATCHES_FILE = Path.home() / ".karb" / "crossplatform_matches.json"

security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Verify dashboard credentials."""
    settings = get_settings()

    correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        settings.dashboard_username.encode("utf8"),
    )
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        settings.dashboard_password.encode("utf8"),
    )

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


def create_app() -> FastAPI:
    """Create the FastAPI dashboard application."""
    app = FastAPI(
        title="Karb Dashboard",
        description="Polymarket Arbitrage Bot Dashboard",
        version="1.0.0",
    )

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        username: str = Depends(verify_credentials),
    ):
        """Main dashboard page."""
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "username": username},
        )

    @app.get("/api/status")
    async def get_status(username: str = Depends(verify_credentials)):
        """Get current bot status."""
        settings = get_settings()

        # Read scanner stats from shared file
        scanner_stats = {
            "markets": 0,
            "price_updates": 0,
            "arbitrage_alerts": 0,
            "ws_connected": False,
            "last_update": None,
        }
        try:
            if STATS_FILE.exists():
                with open(STATS_FILE) as f:
                    scanner_stats = json.load(f)
        except Exception:
            pass

        return {
            "bot": {
                "dry_run": settings.dry_run,
                "min_profit": f"{settings.min_profit_threshold * 100:.1f}%",
                "max_position": f"${settings.max_position_size}",
                "wallet": settings.wallet_address[:10] + "..." if settings.wallet_address else None,
            },
            "scanner": scanner_stats,
            "timestamp": datetime.now().isoformat(),
        }

    @app.get("/api/balance")
    async def get_balance(username: str = Depends(verify_credentials)):
        """Get current balances."""
        tracker = PortfolioTracker()
        try:
            balances = await tracker.get_current_balances()
            return balances
        except Exception as e:
            log.error("Failed to get balances", error=str(e))
            return {"error": str(e)}

    @app.get("/api/trades")
    async def get_trades(
        limit: int = 20,
        username: str = Depends(verify_credentials),
    ):
        """Get recent trades."""
        trade_log = TradeLog()
        trades = trade_log.get_trades(limit=limit)
        return {
            "trades": [
                {
                    "timestamp": t.timestamp,
                    "platform": t.platform,
                    "market": t.market_question[:50] + "..." if len(t.market_question) > 50 else t.market_question,
                    "side": t.side,
                    "price": float(t.price),
                    "size": float(t.size),
                    "cost": float(t.cost),
                    "status": t.status,
                }
                for t in trades
            ],
            "count": len(trades),
        }

    @app.get("/api/pnl")
    async def get_pnl(username: str = Depends(verify_credentials)):
        """Get profit/loss summary."""
        trade_log = TradeLog()
        tracker = PortfolioTracker()

        daily = trade_log.get_daily_summary()
        all_time = trade_log.get_all_time_summary()
        pnl = tracker.get_profit_loss()

        return {
            "daily": daily,
            "all_time": all_time,
            "portfolio": pnl,
        }

    @app.get("/api/alerts")
    async def get_alerts(
        limit: int = 10,
        username: str = Depends(verify_credentials),
    ):
        """Get recent arbitrage alerts."""
        alerts = []
        try:
            if ALERTS_FILE.exists():
                with open(ALERTS_FILE) as f:
                    alerts = json.load(f)
        except Exception:
            pass

        return {"alerts": alerts[-limit:], "count": len(alerts)}

    @app.get("/api/crossplatform/matches")
    async def get_crossplatform_matches(
        refresh: bool = False,
        username: str = Depends(verify_credentials),
    ):
        """
        Get cross-platform matched markets between Polymarket and Kalshi.

        Args:
            refresh: If True, fetch fresh data from both platforms. Otherwise use cached data.
        """
        # Try to load cached matches first
        cached_matches = []
        cache_time = None

        try:
            if MATCHES_FILE.exists() and not refresh:
                with open(MATCHES_FILE) as f:
                    cache_data = json.load(f)
                    cached_matches = cache_data.get("matches", [])
                    cache_time = cache_data.get("timestamp")
        except Exception:
            pass

        # Return cached data if available and not refreshing
        if cached_matches and not refresh:
            return {
                "matches": cached_matches,
                "count": len(cached_matches),
                "cached": True,
                "timestamp": cache_time,
            }

        # Fetch fresh data
        try:
            from karb.api.gamma import GammaClient
            from karb.api.kalshi import KalshiClient

            # Use LLM matcher if API key is available, otherwise fall back to fuzzy
            provider = settings.llm_provider
            if provider == "anthropic" and settings.anthropic_api_key:
                from karb.matcher.llm_matcher import LLMMatcher
                matcher = LLMMatcher(provider="anthropic", api_key=settings.anthropic_api_key)
                log.info("Using LLM matcher", provider="anthropic")
            elif provider == "openai" and settings.openai_api_key:
                from karb.matcher.llm_matcher import LLMMatcher
                matcher = LLMMatcher(provider="openai", api_key=settings.openai_api_key)
                log.info("Using LLM matcher", provider="openai")
            elif provider == "gemini" and settings.google_api_key:
                from karb.matcher.llm_matcher import LLMMatcher
                matcher = LLMMatcher(provider="gemini", api_key=settings.google_api_key)
                log.info("Using LLM matcher", provider="gemini")
            else:
                from karb.matcher.event_matcher import EventMatcher
                matcher = EventMatcher(min_confidence=0.5)
                log.info("Using fuzzy matcher (no LLM API key configured)")

            gamma = GammaClient()
            kalshi = KalshiClient()

            # Load markets from both platforms
            poly_markets, kalshi_markets = await asyncio.gather(
                gamma.fetch_all_active_markets(min_liquidity=1000.0),
                kalshi.get_markets(status="open", limit=500),
                return_exceptions=True,
            )

            # Handle errors
            if isinstance(poly_markets, Exception):
                log.error("Failed to fetch Polymarket", error=str(poly_markets))
                poly_markets = []
            if isinstance(kalshi_markets, Exception):
                log.error("Failed to fetch Kalshi", error=str(kalshi_markets))
                kalshi_markets = []

            # Sort poly by liquidity, take top 200
            if poly_markets:
                poly_markets = sorted(poly_markets, key=lambda m: m.liquidity, reverse=True)[:200]

            # Match markets
            matches = []
            if poly_markets and kalshi_markets:
                matched_events = matcher.match_batch(poly_markets, kalshi_markets)

                for m in matched_events:
                    poly_yes = float(m.poly_yes_ask) if m.poly_yes_ask else None
                    kalshi_yes = float(m.kalshi_yes_ask) if m.kalshi_yes_ask else None
                    spread = None
                    if poly_yes and kalshi_yes:
                        spread = round(abs(kalshi_yes - poly_yes) * 100, 2)

                    matches.append({
                        "polymarket": {
                            "question": m.polymarket.question,
                            "id": m.polymarket.id,
                            "yes_price": poly_yes,
                            "liquidity": float(m.polymarket.liquidity),
                        },
                        "kalshi": {
                            "ticker": m.kalshi.ticker,
                            "title": m.kalshi.title,
                            "yes_price": kalshi_yes,
                        },
                        "confidence": round(m.confidence, 2),
                        "match_type": m.match_type,
                        "spread_pct": spread,
                        "reasoning": getattr(m, 'reasoning', ''),
                    })

            # Sort by confidence
            matches.sort(key=lambda x: x["confidence"], reverse=True)

            # Cache the results
            try:
                MATCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(MATCHES_FILE, "w") as f:
                    json.dump({
                        "matches": matches,
                        "timestamp": datetime.now().isoformat(),
                        "poly_count": len(poly_markets) if poly_markets else 0,
                        "kalshi_count": len(kalshi_markets) if kalshi_markets else 0,
                    }, f)
            except Exception as e:
                log.debug("Failed to cache matches", error=str(e))

            # Close clients
            await gamma.close()
            await kalshi.close()

            return {
                "matches": matches,
                "count": len(matches),
                "cached": False,
                "timestamp": datetime.now().isoformat(),
                "poly_markets": len(poly_markets) if poly_markets else 0,
                "kalshi_markets": len(kalshi_markets) if kalshi_markets else 0,
            }

        except Exception as e:
            log.error("Cross-platform matching failed", error=str(e))
            return {
                "matches": [],
                "count": 0,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

    return app


def run_dashboard(host: str = "0.0.0.0", port: int = 8080):
    """Run the dashboard server."""
    import uvicorn

    settings = get_settings()

    if not settings.dashboard_username or not settings.dashboard_password:
        log.error("Dashboard credentials not configured. Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD in .env")
        return

    log.info("Starting dashboard", host=host, port=port)

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
