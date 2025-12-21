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
