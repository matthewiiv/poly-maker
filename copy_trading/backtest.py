"""
Historical backtest for the whale scanner.

Replays weeks of Polymarket's large-trade tape through the exact same
bucketing + scoring logic the live scanner uses, with wallet profiles
reconstructed *as of each fill's timestamp* (no lookahead: the oldest-first
/activity feed lets us see precisely what the wallet's history looked like
at the moment it traded).

Every bucket that crosses the cash threshold becomes a *candidate* and gets
a score; candidates at or above the alert threshold are the signals the live
scanner would have fired on. Outcomes are then measured against market
resolutions (or current prices for still-open markets), so we can ask the
two questions that matter:

  1. Would the scanner have caught a specific whale (and how fast)?
  2. Do high-scoring "fresh wallet bets big" signals actually win more
     often than ordinary big bets — i.e. do they look informed?

Usage:
    uv run python -m copy_trading.backtest                     # full sweep
    uv run python -m copy_trading.backtest --case-study 0x9cb2…  # one market
    uv run python -m copy_trading.backtest --json results.json

All data comes from public endpoints and is cached on disk, so re-runs and
parameter sweeps are cheap.
"""

import argparse
import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from copy_trading.config import CopyConfig
from copy_trading.data_api import ACTIVITY_PAGE_LIMIT, DATA_API, _get
from copy_trading.signals import WalletProfile, WhaleSignal, score_whale

GAMMA_API = "https://gamma-api.polymarket.com"

# The Data API rejects offsets past ~10k; one sweep at a $25k cash filter
# reaches back roughly 50 days.
MAX_TAPE_OFFSET = 10_000
PAGE = 100

DEFAULT_CACHE_DIR = "copy_trading/backtest_cache"


# ---------------------------------------------------------------------------
# Cached data collection
# ---------------------------------------------------------------------------


def _cached(cache_dir: str, name: str, fetch):
    path = os.path.join(cache_dir, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    data = fetch()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def fetch_tape(min_cash: float, cache_dir: str, max_offset: int = MAX_TAPE_OFFSET) -> List[dict]:
    """All reachable tape fills >= min_cash, oldest first, deduped."""

    def sweep():
        fills = []
        for offset in range(0, max_offset + 1, PAGE):
            page = _get(
                f"{DATA_API}/trades",
                {
                    "limit": PAGE,
                    "offset": offset,
                    "takerOnly": "true",
                    "filterType": "CASH",
                    "filterAmount": min_cash,
                },
            )
            if not page:
                break
            fills.extend(page)
            if len(page) < PAGE:
                break
        return fills

    fills = _cached(cache_dir, f"tape_{int(min_cash)}.json", sweep)
    seen, unique = set(), []
    for t in fills:
        key = (t.get("transactionHash"), t["asset"], t["side"], t["size"])
        if key not in seen:
            seen.add(key)
            unique.append(t)
    unique.sort(key=lambda t: int(t["timestamp"]))
    return unique


def fetch_market_tape(condition_id: str, min_cash: float, cache_dir: str) -> List[dict]:
    """Tape for a single market (its own pagination budget -> deeper history)."""

    def sweep():
        fills = []
        for offset in range(0, MAX_TAPE_OFFSET + 1, PAGE):
            page = _get(
                f"{DATA_API}/trades",
                {
                    "market": condition_id,
                    "limit": PAGE,
                    "offset": offset,
                    "takerOnly": "true",
                    "filterType": "CASH",
                    "filterAmount": min_cash,
                },
            )
            if not page:
                break
            fills.extend(page)
            if len(page) < PAGE:
                break
        return fills

    fills = _cached(cache_dir, f"market_{condition_id[:18]}_{int(min_cash)}.json", sweep)
    fills.sort(key=lambda t: int(t["timestamp"]))
    return fills


def fetch_activity(wallet: str, cache_dir: str) -> Optional[List[dict]]:
    def fetch():
        return (
            _get(
                f"{DATA_API}/activity",
                {"user": wallet, "limit": ACTIVITY_PAGE_LIMIT, "sortDirection": "ASC"},
            )
            or []
        )

    try:
        return _cached(cache_dir, f"activity/{wallet}.json", fetch)
    except Exception as ex:  # persistent API failure: exclude wallet, keep going
        print(f"[backtest] activity fetch failed for {wallet}: {ex}")
        return None


def fetch_activities(wallets: List[str], cache_dir: str, workers: int = 6) -> Dict[str, list]:
    out: Dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for wallet, act in zip(wallets, pool.map(lambda w: fetch_activity(w, cache_dir), wallets)):
            if act is not None:
                out[wallet] = act
    return out


def fetch_markets(condition_ids: List[str], cache_dir: str) -> Dict[str, dict]:
    """conditionId -> {resolved, prices per outcomeIndex, question, closed}."""

    def fetch_batch(batch):
        # Gamma excludes closed markets from condition_ids lookups unless
        # closed=true is passed, so resolved markets need a second query.
        qs = "&".join(f"condition_ids={c}" for c in batch)
        rows = list(_get(f"{GAMMA_API}/markets?{qs}&limit=100") or [])
        rows += _get(f"{GAMMA_API}/markets?{qs}&closed=true&limit=100") or []
        return rows

    out: Dict[str, dict] = {}
    todo = sorted(set(condition_ids))
    for i in range(0, len(todo), 20):
        batch = todo[i : i + 20]
        key = f"markets_v2/{batch[0][:14]}_{len(batch)}.json"
        try:
            rows = _cached(cache_dir, key, lambda b=batch: fetch_batch(b))
        except Exception as ex:
            print(f"[backtest] gamma fetch failed for batch {i}: {ex}")
            continue
        for m in rows or []:
            try:
                prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
            except (ValueError, TypeError):
                prices = []
            resolved = bool(m.get("closed")) and (
                m.get("umaResolutionStatus") == "resolved" or (prices and max(prices) >= 0.999)
            )
            out[m["conditionId"]] = {
                "question": m.get("question"),
                "closed": bool(m.get("closed")),
                "resolved": resolved,
                "prices": prices,
            }
    return out


# ---------------------------------------------------------------------------
# Point-in-time profiling and replay
# ---------------------------------------------------------------------------


def profile_at(wallet: str, activity: List[dict], ts: int) -> WalletProfile:
    """
    The wallet's profile as it looked at time ts (inclusive, matching the
    live scanner which polls seconds after the fill prints).

    The activity feed is oldest-first, so for any wallet whose lifetime
    record count is under the page cap — and for older wallets, whenever
    fewer than a full page of records predate ts — the pre-ts view is
    complete, not truncated.
    """
    before = [a for a in activity if int(a["timestamp"]) <= ts]
    trades = [a for a in before if a.get("type") == "TRADE"]
    markets = {a.get("conditionId") for a in before if a.get("conditionId")}
    return WalletProfile(
        wallet=wallet,
        first_seen_ts=int(before[0]["timestamp"]) if before else None,
        activity_count=len(before),
        trade_count=len(trades),
        total_cash_traded=sum(float(t.get("usdcSize") or 0) for t in trades),
        markets_traded=len(markets),
        capped=len(activity) >= ACTIVITY_PAGE_LIMIT and len(before) >= ACTIVITY_PAGE_LIMIT,
    )


@dataclass
class Candidate:
    """A bucket that crossed the cash threshold during replay."""

    wallet: str
    name: str
    asset: str
    condition_id: str
    side: str
    outcome: str
    outcome_index: int
    title: str
    slug: str
    eval_ts: int
    avg_price: float  # bucket avg at evaluation time (what a copier would chase)
    cash_at_eval: float
    final_cash: float = 0.0  # bucket total after all fills
    fills: int = 0
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    wallet_age_days: Optional[float] = None
    wallet_trades_before: int = 0
    profile_capped: bool = False
    # outcome fields (filled by evaluate)
    market_resolved: bool = False
    won: Optional[bool] = None
    exit_price: Optional[float] = None
    entry_price: Optional[float] = None
    ret_per_dollar: Optional[float] = None
    category: str = "other"


CRYPTO_WORDS = ("bitcoin", "btc", "ethereum", "eth ", "solana", "xrp", "crypto", "doge")
POLITICS_WORDS = (
    "act ",
    "act?",
    "law",
    "senate",
    "congress",
    "president",
    "trump",
    "election",
    "fed ",
    "nominee",
    "shutdown",
    "tariff",
    "government",
    "mayor",
    "impeach",
)
SPORTS_WORDS = (
    " vs",
    "win on 20",
    "epl",
    "nba",
    "nfl",
    "mlb",
    "atp",
    "wta",
    "ufc",
    " fc ",
    "grand prix",
    "open,",
    "league",
    "match",
)


def categorize(title: str, slug: str) -> str:
    text = f"{title} {slug}".lower()
    if any(w in text for w in SPORTS_WORDS):
        return "sports"
    if any(w in text for w in CRYPTO_WORDS):
        return "crypto"
    if any(w in text for w in POLITICS_WORDS):
        return "politics"
    return "other"


def replay(
    fills: List[dict],
    activities: Dict[str, list],
    cfg: CopyConfig,
) -> List[Candidate]:
    """
    Feed fills (oldest first) through the scanner's bucketing logic. Each
    bucket is scored once, at the first fill where its cash crosses
    cfg.min_trade_cash — exactly when the live scanner would have evaluated
    it — using the wallet's profile as of that moment.
    """
    buckets: Dict[str, WhaleSignal] = {}
    evaluated: Dict[str, Candidate] = {}

    for trade in fills:
        wallet = trade["proxyWallet"].lower()
        key = f"{wallet}:{trade['asset']}:{trade['side']}"
        ts = int(trade["timestamp"])

        bucket = buckets.get(key)
        if bucket is None or ts - bucket.last_ts > cfg.aggregation_window_secs:
            bucket = WhaleSignal.from_trade(trade)
            buckets[key] = bucket
        else:
            bucket.add_fill(trade)

        bucket_id = f"{key}:{bucket.first_ts}"
        if bucket_id in evaluated:
            evaluated[bucket_id].final_cash = bucket.total_cash
            evaluated[bucket_id].fills = bucket.fill_count
            continue
        # A whale *bet* is a BUY; large sells are mostly winners cashing out
        # (often at ~$1.00) and carry no copyable information.
        if bucket.side != "BUY":
            continue
        if bucket.total_cash < cfg.min_trade_cash:
            continue
        activity = activities.get(wallet)
        if activity is None:
            continue

        profile = profile_at(wallet, activity, ts)
        score, reasons = score_whale(
            bucket.total_cash, bucket.avg_price, bucket.side, profile, cfg, now=ts
        )
        evaluated[bucket_id] = Candidate(
            wallet=wallet,
            name=bucket.name,
            asset=bucket.asset,
            condition_id=bucket.condition_id,
            side=bucket.side,
            outcome=bucket.outcome,
            outcome_index=int(trade.get("outcomeIndex") or 0),
            title=bucket.title,
            slug=trade.get("slug") or "",
            eval_ts=ts,
            avg_price=bucket.avg_price,
            cash_at_eval=bucket.total_cash,
            final_cash=bucket.total_cash,
            fills=bucket.fill_count,
            score=score,
            reasons=reasons,
            wallet_age_days=profile.age_days(ts),
            wallet_trades_before=profile.trade_count,
            profile_capped=profile.capped,
            category=categorize(bucket.title, trade.get("slug") or ""),
        )

    return list(evaluated.values())


def evaluate(
    candidates: List[Candidate],
    markets: Dict[str, dict],
    slippage_penalty: float,
) -> None:
    """
    Attach outcomes. BUY candidates only (the copier never shorts): entry is
    the whale's average price plus a slippage penalty (we're always late);
    exit is the resolved outcome (1/0) or the current price for open markets.
    """
    for c in candidates:
        market = markets.get(c.condition_id)
        if not market or c.side != "BUY":
            continue
        prices = market["prices"]
        if c.outcome_index >= len(prices):
            continue
        c.market_resolved = market["resolved"]
        c.exit_price = prices[c.outcome_index]
        c.entry_price = min(round(c.avg_price + slippage_penalty, 4), 0.999)
        c.ret_per_dollar = (c.exit_price / c.entry_price) - 1.0
        if c.market_resolved:
            c.won = c.exit_price >= 0.5


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _stats(group: List[Candidate]) -> dict:
    priced = [c for c in group if c.ret_per_dollar is not None]
    resolved = [c for c in priced if c.market_resolved]
    open_ = [c for c in priced if not c.market_resolved]
    wins = [c for c in resolved if c.won]

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "candidates": len(group),
        "priced_buys": len(priced),
        "resolved": len(resolved),
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "avg_ret_resolved": avg([c.ret_per_dollar for c in resolved]),
        "open": len(open_),
        "avg_ret_open_mtm": avg([c.ret_per_dollar for c in open_]),
        "avg_ret_all": avg([c.ret_per_dollar for c in priced]),
    }


def summarize(candidates: List[Candidate], cfg: CopyConfig) -> Dict[str, dict]:
    alerts = [c for c in candidates if c.score >= cfg.alert_score]
    rows = {
        "ALL candidates": _stats(candidates),
        f"ALERTS (score>={cfg.alert_score})": _stats(alerts),
        "  score 90-100": _stats([c for c in candidates if c.score >= 90]),
        "  score 75-89": _stats([c for c in candidates if 75 <= c.score < 90]),
        "  score 60-74": _stats([c for c in candidates if 60 <= c.score < 75]),
        "CONTROL score<60 (veteran big bets)": _stats([c for c in candidates if c.score < 60]),
        "  fresh<3d wallets": _stats(
            [
                c
                for c in candidates
                if c.wallet_age_days is not None and c.wallet_age_days <= 3 and not c.profile_capped
            ]
        ),
        "  veteran (capped history)": _stats([c for c in candidates if c.profile_capped]),
    }
    for cat in ("politics", "crypto", "sports", "other"):
        rows[f"  alerts:{cat}"] = _stats([c for c in alerts if c.category == cat])
    return rows


def format_table(rows: Dict[str, dict]) -> str:
    header = (
        f"{'group':38} {'n':>5} {'buys':>5} {'rslvd':>5} {'win%':>6} "
        f"{'ret/$(r)':>9} {'open':>5} {'mtm/$':>7} {'ret/$all':>9}"
    )
    lines = [header, "-" * len(header)]
    for name, s in rows.items():

        def pct(x):
            return f"{100*x:.1f}%" if x is not None else "-"

        def num(x):
            return f"{x:+.3f}" if x is not None else "-"

        lines.append(
            f"{name:38} {s['candidates']:>5} {s['priced_buys']:>5} {s['resolved']:>5} "
            f"{pct(s['win_rate']):>6} {num(s['avg_ret_resolved']):>9} {s['open']:>5} "
            f"{num(s['avg_ret_open_mtm']):>7} {num(s['avg_ret_all']):>9}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Case study: replay every whale on one market
# ---------------------------------------------------------------------------


def case_study(condition_id: str, min_cash: float, cfg: CopyConfig, cache_dir: str) -> None:
    fills = fetch_market_tape(condition_id, min_cash=20_000, cache_dir=cache_dir)
    wallets = sorted({t["proxyWallet"].lower() for t in fills})
    activities = fetch_activities(wallets, cache_dir)
    markets = fetch_markets([condition_id], cache_dir)
    market = markets.get(condition_id, {})
    print(f"\n=== CASE STUDY: {market.get('question') or condition_id} ===")
    print(
        f"{len(fills)} fills >= $20k from {len(wallets)} wallets; "
        f"current prices {market.get('prices')}\n"
    )

    candidates = replay(fills, activities, cfg)
    evaluate(candidates, markets, slippage_penalty=0.01)
    for c in sorted(candidates, key=lambda c: c.eval_ts):
        age = f"{c.wallet_age_days:.2f}d" if c.wallet_age_days is not None else "?"
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(c.eval_ts))
        verdict = "ALERT" if c.score >= cfg.alert_score else "no alert"
        mtm = f"{c.ret_per_dollar:+.1%}" if c.ret_per_dollar is not None else "n/a"
        print(
            f"[{when} UTC] {verdict:8} score={c.score:<3} {c.name or c.wallet[:10]:16} "
            f"{c.side} {c.outcome:3} @ {c.avg_price:.3f} | ${c.final_cash:,.0f} "
            f"({c.fills} fills) | wallet {age} old, {c.wallet_trades_before} prior trades "
            f"| copy mtm {mtm} | {', '.join(c.reasons)}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backtest the whale scanner on historical tape")
    p.add_argument("--min-cash", type=float, default=25_000.0)
    p.add_argument("--alert-score", type=int, default=60)
    p.add_argument("--fresh-days", type=float, default=3.0)
    p.add_argument(
        "--slippage-penalty",
        type=float,
        default=0.01,
        help="assumed entry price disadvantage vs the whale's fill",
    )
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    p.add_argument(
        "--case-study",
        default=None,
        metavar="CONDITION_ID",
        help="replay all big wallets on one market instead of the full sweep",
    )
    p.add_argument(
        "--sweep", action="store_true", help="also run an alert-score x slippage sensitivity sweep"
    )
    p.add_argument("--json", default=None, help="write all candidates to this JSON file")
    args = p.parse_args(argv)

    cfg = CopyConfig(
        min_trade_cash=args.min_cash,
        alert_score=args.alert_score,
        fresh_wallet_days=args.fresh_days,
    )

    if args.case_study:
        case_study(args.case_study, args.min_cash, cfg, args.cache_dir)
        return 0

    print(f"[backtest] fetching tape (>= ${args.min_cash:,.0f})...")
    fills = fetch_tape(args.min_cash, args.cache_dir)
    if not fills:
        print("[backtest] no fills fetched")
        return 1
    t0, t1 = int(fills[0]["timestamp"]), int(fills[-1]["timestamp"])
    days = (t1 - t0) / 86400
    wallets = sorted({t["proxyWallet"].lower() for t in fills})
    print(
        f"[backtest] {len(fills)} fills over {days:.1f} days "
        f"({time.strftime('%Y-%m-%d', time.gmtime(t0))} -> "
        f"{time.strftime('%Y-%m-%d', time.gmtime(t1))}), {len(wallets)} wallets"
    )

    print(f"[backtest] profiling {len(wallets)} wallets (cached after first run)...")
    activities = fetch_activities(wallets, args.cache_dir)

    candidates = replay(fills, activities, cfg)
    conditions = sorted({c.condition_id for c in candidates if c.condition_id})
    print(
        f"[backtest] {len(candidates)} candidate buckets across {len(conditions)} markets; "
        f"fetching resolutions..."
    )
    markets = fetch_markets(conditions, args.cache_dir)
    evaluate(candidates, markets, args.slippage_penalty)

    print(
        f"\n=== BACKTEST {time.strftime('%Y-%m-%d', time.gmtime(t0))} -> "
        f"{time.strftime('%Y-%m-%d', time.gmtime(t1))} "
        f"(min ${args.min_cash:,.0f}, alert>={args.alert_score}, "
        f"slippage +{args.slippage_penalty:.2f}) ===\n"
    )
    print(format_table(summarize(candidates, cfg)))

    alerts = [c for c in candidates if c.score >= cfg.alert_score]
    alerts.sort(key=lambda c: -c.final_cash)
    print(f"\nTop alerts by whale size ({min(15, len(alerts))} of {len(alerts)}):")
    for c in alerts[:15]:
        when = time.strftime("%m-%d %H:%M", time.gmtime(c.eval_ts))
        ret = f"{c.ret_per_dollar:+.1%}" if c.ret_per_dollar is not None else "  n/a"
        status = "RESOLVED" if c.market_resolved else "open"
        age = f"{c.wallet_age_days:.1f}d" if c.wallet_age_days is not None else "?"
        print(
            f"  [{when}] {c.name or c.wallet[:10]:16} {c.side} {c.outcome:12.12} "
            f"@ {c.avg_price:.2f} ${c.final_cash:>9,.0f} score={c.score:<3} age={age:>6} "
            f"copy:{ret:>7} {status:8} | {c.title[:44]}"
        )

    if args.sweep:
        print("\n=== SENSITIVITY SWEEP (resolved-only avg return per $1 copied) ===")
        print(f"{'alert>=':>8} {'slip':>6} {'alerts':>7} {'resolved':>8} {'win%':>7} {'ret/$':>8}")
        for score_min in (60, 75, 90):
            for slip in (0.0, 0.01, 0.02, 0.03):
                evaluate(candidates, markets, slip)
                grp = [c for c in candidates if c.score >= score_min]
                s = _stats(grp)
                win = f"{100*s['win_rate']:.1f}%" if s["win_rate"] is not None else "-"
                ret = f"{s['avg_ret_resolved']:+.3f}" if s["avg_ret_resolved"] is not None else "-"
                print(
                    f"{score_min:>8} {slip:>6.2f} {s['candidates']:>7} "
                    f"{s['resolved']:>8} {win:>7} {ret:>8}"
                )
        evaluate(candidates, markets, args.slippage_penalty)  # restore

    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(c) for c in candidates], f, indent=1)
        print(f"\n[backtest] wrote {len(candidates)} candidates to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
