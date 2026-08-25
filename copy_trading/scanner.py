"""
The polling loop that watches Polymarket's trade tape for insiders.

Two detection paths:

1. Unknown wallets: every fill on the platform worth >= min_trade_cash is
   bucketed (wallet/token/side within a short window), the wallet is profiled
   via its activity history, and the bucket is scored. Scores above
   alert_score raise an alert, auto-add the wallet to the watchlist, and —
   when copying is enabled — mirror the position.

2. Watchlist wallets: all their fills (down to follow_min_cash) are tracked,
   so their follow-up buys are copied and their exits close our copies.
"""

import time
import traceback
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests

from copy_trading.config import CopyConfig
from copy_trading.data_api import get_large_trades, get_wallet_trades, trade_cash
from copy_trading.executor import CopyExecutor
from copy_trading.market_class import MarketClassifier
from copy_trading.signals import WalletProfile, InsiderSignal, profile_wallet, score_insider
from copy_trading.state import StateStore

# Ignore tape entries older than this by default — copying an insider minutes
# late is already marginal; copying one from hours ago is just buying the move.
DEFAULT_MAX_FILL_AGE_SECS = 900.0

PROFILE_CACHE_TTL_SECS = 600.0


class InsiderScanner:
    """
    Args:
        cfg: thresholds and copy parameters
        state: persistent store (dedupe, watchlist, exposure)
        executor: place/plan copy orders; None = alert-only mode
        max_fill_age_secs: how far back in the tape to consider fills
        profile_fn / trades_fn / wallet_trades_fn: injectable for tests
    """

    def __init__(
        self,
        cfg: CopyConfig,
        state: StateStore,
        executor: Optional[CopyExecutor] = None,
        max_fill_age_secs: float = DEFAULT_MAX_FILL_AGE_SECS,
        profile_fn: Callable[[str], WalletProfile] = profile_wallet,
        trades_fn: Callable[..., List[dict]] = get_large_trades,
        wallet_trades_fn: Callable[..., List[dict]] = get_wallet_trades,
        classifier: Optional[MarketClassifier] = None,
    ):
        self.cfg = cfg
        self.state = state
        self.executor = executor
        self.max_fill_age_secs = max_fill_age_secs
        self.profile_fn = profile_fn
        self.trades_fn = trades_fn
        self.wallet_trades_fn = wallet_trades_fn
        self.classifier = classifier or MarketClassifier()

        self.buckets: Dict[str, InsiderSignal] = {}
        self._profile_cache: Dict[str, Tuple[WalletProfile, float]] = {}
        self._polls = 0

    # -- public API ---------------------------------------------------------
    def seed_watchlist(self, wallets: Iterable[str], source: str = "manual") -> None:
        for w in wallets:
            w = w.strip().lower()
            if w:
                self.state.add_to_watchlist(w, reason="seeded via CLI", source=source)
                print(f"[scanner] watching wallet {w} ({source})")

    def poll_once(self, now: Optional[float] = None) -> int:
        """One pass over the tape (and watchlist wallets). Returns new-fill count."""
        now = now if now is not None else time.time()

        fills = list(self.trades_fn(self.cfg.min_trade_cash, limit=100))
        for wallet in list(self.state.watchlist):
            try:
                fills.extend(self.wallet_trades_fn(wallet, limit=25))
            except Exception:
                print(f"[scanner] failed to fetch trades for watched wallet {wallet}")

        new_fills = 0
        # Oldest first so buckets aggregate in arrival order.
        for trade in sorted(fills, key=lambda t: int(t["timestamp"])):
            try:
                if self._ingest_fill(trade, now):
                    new_fills += 1
            except Exception:
                print("[scanner] error ingesting fill")
                print(traceback.format_exc())

        self._expire_buckets(now)
        if new_fills:
            self.state.save()
        self._polls += 1
        if self._polls % 25 == 0:
            print(
                f"[scanner] heartbeat: {len(self.state.seen)} fills tracked, "
                f"{len(self.buckets)} active buckets, "
                f"{len(self.state.watchlist)} wallets watched, "
                f"${self.state.total_spent:,.2f} committed"
            )
        return new_fills

    def run(self) -> None:
        mode = "copy" if self.executor else "alert-only"
        live = "LIVE" if (self.executor and self.executor.live) else "dry-run"
        print(
            f"[scanner] starting ({mode}, {live}): min fill ${self.cfg.min_trade_cash:,.0f}, "
            f"alert score >= {self.cfg.alert_score}, poll every {self.cfg.poll_secs}s"
        )
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                print("\n[scanner] stopping; saving state")
                self.state.save()
                return
            except Exception:
                print("[scanner] poll failed; retrying")
                print(traceback.format_exc())
            time.sleep(self.cfg.poll_secs)

    # -- internals ----------------------------------------------------------
    def _ingest_fill(self, trade: dict, now: float) -> bool:
        fill_key = (
            f"{trade.get('transactionHash')}:{trade['asset']}:" f"{trade['side']}:{trade['size']}"
        )
        if not self.state.mark_seen(fill_key, now):
            return False

        age = now - int(trade["timestamp"])
        if age > self.max_fill_age_secs:
            return True  # recorded as seen, but too stale to act on

        wallet = trade["proxyWallet"].lower()
        bucket_key = f"{wallet}:{trade['asset']}:{trade['side']}"

        bucket = self.buckets.get(bucket_key)
        if (
            bucket is None
            or int(trade["timestamp"]) - bucket.last_ts > self.cfg.aggregation_window_secs
        ):
            bucket = InsiderSignal.from_trade(trade)
            self.buckets[bucket_key] = bucket
        else:
            bucket.add_fill(trade)

        self._evaluate_bucket(bucket_key, bucket, now)
        return True

    def _evaluate_bucket(self, bucket_key: str, bucket: InsiderSignal, now: float) -> None:
        # One alert/copy per bucket; keyed with first_ts so the same wallet
        # re-hitting the same token hours later is treated as a fresh event.
        alert_key = f"{bucket_key}:{bucket.first_ts}"
        if alert_key in self.state.alerted:
            return

        watched = self.state.is_watched(bucket.wallet)

        if watched and bucket.total_cash >= self.cfg.follow_min_cash:
            self.state.alerted[alert_key] = now
            self._announce(bucket, None, None, ["watchlist-follow"], kind="FOLLOW")
            self._copy(bucket)
            return

        # An insider *bet* is a BUY. Large sells from unknown wallets are mostly
        # winners cashing out (often at ~$1.00) — nothing to copy or watch.
        if bucket.side != "BUY":
            return

        if bucket.total_cash < self.cfg.min_trade_cash:
            return

        # Classify before spending an API call on profiling: in insider-only
        # mode most tape volume (sports, price series) is filtered out here.
        mclass = self.classifier.classify(bucket.event_slug, bucket.title)
        if self.cfg.insider_only and not mclass.insider_plausible:
            return

        profile = self._profile(bucket.wallet)
        if profile is None:
            return  # profiling failed; a later fill in this bucket retries

        score, reasons = score_insider(
            bucket.total_cash, bucket.avg_price, bucket.side, profile, self.cfg, now
        )
        if score < self.cfg.alert_score:
            return
        if mclass.insider_plausible:
            reasons.append(f"insider-market({','.join(mclass.reasons[:2])})")
        else:
            reasons.append(f"market:{mclass.category}")

        self.state.alerted[alert_key] = now
        self.state.add_to_watchlist(
            bucket.wallet,
            reason=f"score {score}: {', '.join(reasons)} on '{bucket.title}'",
            source="auto",
        )
        self._announce(bucket, profile, score, reasons, kind="INSIDER ALERT")
        self._copy(bucket)

    def _copy(self, bucket: InsiderSignal) -> None:
        if not self.executor:
            return
        try:
            plan = self.executor.plan(bucket)
            if plan:
                self.executor.execute(bucket, plan)
        except Exception:
            print("[scanner] copy failed")
            print(traceback.format_exc())

    def _profile(self, wallet: str) -> Optional[WalletProfile]:
        cached = self._profile_cache.get(wallet)
        if cached and time.time() - cached[1] < PROFILE_CACHE_TTL_SECS:
            return cached[0]
        try:
            profile = self.profile_fn(wallet)
        except Exception as ex:
            print(f"[scanner] could not profile {wallet}: {ex}")
            return None
        self._profile_cache[wallet] = (profile, time.time())
        return profile

    def _expire_buckets(self, now: float) -> None:
        horizon = self.cfg.aggregation_window_secs * 2
        for key in list(self.buckets):
            if now - self.buckets[key].last_ts > horizon:
                del self.buckets[key]

    def _announce(
        self,
        bucket: InsiderSignal,
        profile: Optional[WalletProfile],
        score: Optional[int],
        reasons: List[str],
        kind: str,
    ) -> None:
        who = bucket.name or "anon"
        wallet_short = f"{bucket.wallet[:6]}…{bucket.wallet[-4:]}"
        wallet_bits = ""
        if profile is not None:
            age = profile.age_days()
            age_txt = f"{age:.1f}d old" if age is not None else "age unknown"
            more = "+" if profile.capped else ""
            wallet_bits = f" | wallet {age_txt}, {profile.trade_count}{more} lifetime trades"
        score_txt = f" score={score}" if score is not None else ""

        text = (
            f"🕵️ {kind}{score_txt} | {who} ({wallet_short}) "
            f"{bucket.side} '{bucket.outcome}' @ {bucket.avg_price:.3f} — "
            f"${bucket.total_cash:,.0f} across {bucket.fill_count} fill(s) | "
            f"{bucket.title}{wallet_bits} | "
            f"https://polymarket.com/event/{bucket.event_slug} | "
            f"reasons: {', '.join(reasons)}"
        )
        print(f"[{time.strftime('%H:%M:%S')}] {text}")
        self._webhook(text)

    def _webhook(self, text: str) -> None:
        if not self.cfg.webhook_url:
            return
        try:
            # "content" is what Discord reads, "text" is what Slack reads.
            requests.post(self.cfg.webhook_url, json={"content": text, "text": text}, timeout=5)
        except Exception as ex:
            print(f"[scanner] webhook failed: {ex}")
