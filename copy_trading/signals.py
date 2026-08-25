"""
Wallet profiling and insider-signal scoring.

The scoring is deliberately simple and transparent: a handful of additive
heuristics that describe the pattern from the news stories — a wallet that
did not exist last week suddenly puts six figures on one outcome.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from copy_trading.config import CopyConfig
from copy_trading.data_api import ACTIVITY_PAGE_LIMIT, get_wallet_activity


@dataclass
class WalletProfile:
    """Summary of a wallet's on-platform history."""

    wallet: str
    first_seen_ts: Optional[int]  # None if the wallet has no visible history
    activity_count: int
    trade_count: int
    total_cash_traded: float
    markets_traded: int
    # True when the history hit the API page cap, i.e. an established account
    # whose real first-seen date may be even older than we can see.
    capped: bool

    def age_days(self, now: Optional[float] = None) -> Optional[float]:
        if self.first_seen_ts is None:
            return None
        now = now if now is not None else time.time()
        return max(0.0, (now - self.first_seen_ts) / 86400.0)


def profile_wallet(wallet: str, activity: Optional[List[dict]] = None) -> WalletProfile:
    """
    Build a WalletProfile from the wallet's oldest-first activity feed.

    Pass `activity` to profile from already-fetched records (used in tests);
    otherwise the Data API is queried.
    """
    if activity is None:
        activity = get_wallet_activity(wallet)

    trades = [a for a in activity if a.get("type") == "TRADE"]
    markets = {a.get("conditionId") for a in activity if a.get("conditionId")}

    return WalletProfile(
        wallet=wallet,
        first_seen_ts=int(activity[0]["timestamp"]) if activity else None,
        activity_count=len(activity),
        trade_count=len(trades),
        total_cash_traded=sum(float(t.get("usdcSize") or 0) for t in trades),
        markets_traded=len(markets),
        capped=len(activity) >= ACTIVITY_PAGE_LIMIT,
    )


def score_insider(
    total_cash: float,
    avg_price: float,
    side: str,
    profile: WalletProfile,
    cfg: CopyConfig,
    now: Optional[float] = None,
) -> Tuple[int, List[str]]:
    """
    Score a (possibly aggregated) trade 0-100. Higher = more like the
    "fresh wallet bets big with conviction" pattern.

    Returns (score, human-readable reasons).
    """
    score = 0
    reasons: List[str] = []

    age = profile.age_days(now)
    if not profile.capped and age is not None:
        if age <= cfg.fresh_wallet_days:
            score += 40
            reasons.append(f"fresh-wallet({age:.1f}d)")
        elif age <= cfg.young_wallet_days:
            score += 20
            reasons.append(f"young-wallet({age:.1f}d)")

    if not profile.capped and profile.trade_count <= cfg.few_trades_threshold:
        score += 15
        reasons.append(f"few-trades({profile.trade_count})")

    if not profile.capped and profile.markets_traded <= 2:
        score += 10
        reasons.append(f"concentrated({profile.markets_traded}-mkt)")

    if total_cash >= cfg.big_bet_usdc:
        score += 25
        reasons.append(f"big-bet(${total_cash:,.0f})")
    elif total_cash >= cfg.big_bet_usdc / 4:
        score += 15
        reasons.append(f"large-bet(${total_cash:,.0f})")
    elif total_cash >= cfg.min_trade_cash:
        score += 5
        reasons.append(f"notable-bet(${total_cash:,.0f})")

    # Buying an outcome that is already likely (but not a lock) reads as
    # conviction rather than a cheap lottery ticket.
    if side == "BUY" and 0.50 <= avg_price <= 0.95:
        score += 10
        reasons.append(f"conviction-buy@{avg_price:.2f}")

    return min(score, 100), reasons


@dataclass
class InsiderSignal:
    """
    An aggregated view of one wallet hitting one token on one side —
    insiders rarely fill in a single print, so nearby fills are bucketed.
    """

    wallet: str
    name: str
    asset: str
    condition_id: str
    side: str
    outcome: str
    title: str
    event_slug: str
    total_cash: float = 0.0
    total_shares: float = 0.0
    fill_count: int = 0
    first_ts: int = 0
    last_ts: int = 0
    tx_hashes: List[str] = field(default_factory=list)

    @property
    def avg_price(self) -> float:
        return self.total_cash / self.total_shares if self.total_shares else 0.0

    def add_fill(self, trade: dict) -> None:
        size = float(trade["size"])
        cash = size * float(trade["price"])
        ts = int(trade["timestamp"])
        self.total_shares += size
        self.total_cash += cash
        self.fill_count += 1
        self.first_ts = ts if not self.first_ts else min(self.first_ts, ts)
        self.last_ts = max(self.last_ts, ts)
        tx = trade.get("transactionHash")
        if tx:
            self.tx_hashes.append(tx)

    @classmethod
    def from_trade(cls, trade: dict) -> "InsiderSignal":
        sig = cls(
            wallet=trade["proxyWallet"],
            name=trade.get("name") or trade.get("pseudonym") or "",
            asset=str(trade["asset"]),
            condition_id=trade.get("conditionId") or "",
            side=trade["side"],
            outcome=trade.get("outcome") or "",
            title=trade.get("title") or "",
            event_slug=trade.get("eventSlug") or "",
        )
        sig.add_fill(trade)
        return sig
