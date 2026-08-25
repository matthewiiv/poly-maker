"""
Thin wrappers over Polymarket's public HTTP APIs.

Everything here is unauthenticated and read-only:

- Data API (https://data-api.polymarket.com):
    /trades    - platform-wide trade tape; supports filtering by minimum cash
                 value, which is what makes whale-spotting a one-liner
    /activity  - a wallet's full on-platform history (trades, splits, merges,
                 redeems); the earliest record dates the wallet
    /positions - a wallet's current open positions
    /value     - a wallet's current portfolio value

- CLOB API (https://clob.polymarket.com):
    /book      - the live order book for a token, including tick size,
                 minimum order size and the neg_risk flag needed to place
                 a mirroring order
"""

import time
from typing import Any, Dict, List, Optional, Tuple

import requests

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# The Data API caps /activity at 500 records per request; if we get a full
# page the wallet is an established account, not a fresh one.
ACTIVITY_PAGE_LIMIT = 500

_session = requests.Session()


def _get(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 15.0):
    """GET with small retry; raises on persistent failure."""
    last_err = None
    for attempt in range(3):
        try:
            resp = _session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as ex:  # noqa: BLE001 - bubble the last error up
            last_err = ex
            time.sleep(0.5 * (attempt + 1))
    raise last_err


def get_large_trades(min_cash: float, limit: int = 100, offset: int = 0) -> List[dict]:
    """
    Newest-first fills across the whole platform worth at least min_cash USDC.

    Each record includes proxyWallet, side, asset (token id), conditionId,
    size (shares), price, timestamp, outcome, title/slug and transactionHash.
    """
    params = {
        "limit": limit,
        "offset": offset,
        "takerOnly": "true",
        "filterType": "CASH",
        "filterAmount": min_cash,
    }
    return _get(f"{DATA_API}/trades", params) or []


def get_wallet_trades(wallet: str, limit: int = 50) -> List[dict]:
    """Newest-first fills for one wallet (no size filter)."""
    params = {"user": wallet, "limit": limit, "takerOnly": "true"}
    return _get(f"{DATA_API}/trades", params) or []


def get_wallet_activity(wallet: str, limit: int = ACTIVITY_PAGE_LIMIT) -> List[dict]:
    """
    Oldest-first on-platform activity for a wallet.

    The first record's timestamp is when the wallet first touched Polymarket,
    which is how we tell a brand-new wallet from a veteran.
    """
    params = {"user": wallet, "limit": limit, "sortDirection": "ASC"}
    return _get(f"{DATA_API}/activity", params) or []


def get_wallet_positions(wallet: str) -> List[dict]:
    """Current open positions for a wallet."""
    return _get(f"{DATA_API}/positions", {"user": wallet}) or []


def get_book(token_id: str) -> dict:
    """
    Live order book for a token. Includes bids, asks, tick_size,
    min_order_size and neg_risk.
    """
    return _get(f"{CLOB_API}/book", {"token_id": str(token_id)})


def best_prices(book: dict) -> Tuple[Optional[float], Optional[float]]:
    """(best_bid, best_ask) from a /book payload; None for an empty side."""
    bids = [float(level["price"]) for level in book.get("bids") or []]
    asks = [float(level["price"]) for level in book.get("asks") or []]
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    return best_bid, best_ask


def trade_cash(trade: dict) -> float:
    """Cash value of a fill in USDC (the tape reports size in shares)."""
    return float(trade["size"]) * float(trade["price"])
