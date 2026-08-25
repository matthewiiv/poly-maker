"""
JSON-file persistence for the whale watcher.

Keeps just enough state to survive restarts without double-alerting or
double-copying: which fills we've already processed, which wallets are on
the watchlist, what we (really or virtually) hold from copying, and how
much cash we've committed per market and overall.
"""

import json
import os
import tempfile
import time
from typing import Dict, Optional

SCHEMA_VERSION = 1

# Forget processed fills after a day; the tape query never reaches back
# that far in practice, so this only bounds file growth.
SEEN_TTL_SECS = 24 * 3600


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self.seen: Dict[str, float] = {}  # fill key -> first-seen unix ts
        self.alerted: Dict[str, float] = {}  # bucket key -> alert unix ts
        self.watchlist: Dict[str, dict] = {}  # wallet -> {added_ts, reason, source}
        # asset (token id) -> {shares, cost_usdc, condition_id, title, outcome}
        self.holdings: Dict[str, dict] = {}
        self.spent_by_market: Dict[str, float] = {}  # condition_id -> committed USDC
        self.total_spent: float = 0.0
        self.total_proceeds: float = 0.0
        self.load()

    # -- persistence --------------------------------------------------------
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as ex:
            print(f"[state] could not read {self.path} ({ex}); starting fresh")
            return
        self.seen = raw.get("seen", {})
        self.alerted = raw.get("alerted", {})
        self.watchlist = raw.get("watchlist", {})
        self.holdings = raw.get("holdings", {})
        self.spent_by_market = raw.get("spent_by_market", {})
        self.total_spent = raw.get("total_spent", 0.0)
        self.total_proceeds = raw.get("total_proceeds", 0.0)

    def save(self) -> None:
        self._prune()
        payload = {
            "v": SCHEMA_VERSION,
            "seen": self.seen,
            "alerted": self.alerted,
            "watchlist": self.watchlist,
            "holdings": self.holdings,
            "spent_by_market": self.spent_by_market,
            "total_spent": self.total_spent,
            "total_proceeds": self.total_proceeds,
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        # Atomic write so a crash mid-save can't corrupt the file.
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=1)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _prune(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        cutoff = now - SEEN_TTL_SECS
        self.seen = {k: ts for k, ts in self.seen.items() if ts >= cutoff}
        self.alerted = {k: ts for k, ts in self.alerted.items() if ts >= cutoff}

    # -- fills --------------------------------------------------------------
    def mark_seen(self, fill_key: str, now: Optional[float] = None) -> bool:
        """Returns True if the fill is new (and records it)."""
        if fill_key in self.seen:
            return False
        self.seen[fill_key] = now if now is not None else time.time()
        return True

    # -- watchlist ----------------------------------------------------------
    def add_to_watchlist(self, wallet: str, reason: str, source: str = "auto") -> None:
        wallet = wallet.lower()
        if wallet not in self.watchlist:
            self.watchlist[wallet] = {
                "added_ts": time.time(),
                "reason": reason,
                "source": source,
            }

    def is_watched(self, wallet: str) -> bool:
        return wallet.lower() in self.watchlist

    # -- exposure / holdings -------------------------------------------------
    def record_buy(
        self, asset: str, condition_id: str, title: str, outcome: str, shares: float, cash: float
    ) -> None:
        pos = self.holdings.setdefault(
            asset,
            {
                "shares": 0.0,
                "cost_usdc": 0.0,
                "condition_id": condition_id,
                "title": title,
                "outcome": outcome,
            },
        )
        pos["shares"] += shares
        pos["cost_usdc"] += cash
        self.spent_by_market[condition_id] = self.spent_by_market.get(condition_id, 0.0) + cash
        self.total_spent += cash

    def record_sell(self, asset: str, shares: float, cash: float) -> None:
        pos = self.holdings.get(asset)
        if not pos:
            return
        pos["shares"] = max(0.0, pos["shares"] - shares)
        self.total_proceeds += cash
        if pos["shares"] <= 1e-9:
            del self.holdings[asset]

    def market_spent(self, condition_id: str) -> float:
        return self.spent_by_market.get(condition_id, 0.0)
