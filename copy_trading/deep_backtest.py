"""
Year-scale backtest over RESOLVED markets only, with exit-strategy simulation.

The global trade tape only paginates back ~50 days, but each market's own
tape has a fresh pagination budget. So this module:

1. Enumerates every closed market above a volume floor for the past N months
   via the Gamma API (monthly windows dodge its offset cap), keeping only
   markets that actually resolved (umaResolutionStatus == resolved).
2. Classifies each market: sports structurally (sportsMarketType /
   gameStartTime on the row — free), price markets by title, the rest via
   cached event-tag lookups (same rules as the live scanner).
3. Sweeps the per-market tape for insider-plausible markets in full, plus a
   deterministic 10% sample of everything else as the control group.
4. Replays fills through the exact scanner logic (point-in-time wallet
   profiles, no lookahead) and grades copies against final resolutions.
5. Simulates exit strategies on each alert's 12h price path: hold to
   resolution, take-profits, stop-losses, combos, time-based exits, and
   following the insider out when they sell.

Usage:
    uv run python -m copy_trading.deep_backtest --months 12
    uv run python -m copy_trading.deep_backtest --months 12 --json out.json

Everything is cached on disk; a full year is ~10k API calls on the first
run and nearly free after.
"""

import argparse
import hashlib
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from copy_trading.backtest import (
    Candidate,
    _cached,
    evaluate,
    fetch_activities,
    format_table,
    replay,
    _stats,
)
from copy_trading.config import CopyConfig
from copy_trading.data_api import CLOB_API, DATA_API, _get
from copy_trading.market_class import PRICE_PATTERN, classify_event

GAMMA_API = "https://gamma-api.polymarket.com"

CONTROL_SAMPLE_MOD = 10  # sweep 1-in-10 non-insider markets as the control
MAX_FILLS_PER_MARKET = 2_000
SLIPPAGE = 0.01


def _gget(url: str, params: Optional[dict] = None, rounds: int = 3):
    """Extra-patient GET: gamma throws transient 422s under sustained load."""
    last = None
    for i in range(rounds):
        try:
            return _get(url, params)
        except Exception as ex:  # noqa: BLE001
            last = ex
            time.sleep(2.0 * (i + 1))
    raise last


# ---------------------------------------------------------------------------
# 1. Enumerate resolved markets
# ---------------------------------------------------------------------------


def month_windows(months: int, now: Optional[float] = None) -> List[Tuple[str, str]]:
    now = now if now is not None else time.time()
    edges = []
    y, m = time.gmtime(now).tm_year, time.gmtime(now).tm_mon
    # First edge is the start of next month so the current partial month is included.
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    edges.append(f"{y2:04d}-{m2:02d}-01T00:00:00Z")
    for _ in range(months + 1):
        edges.append(f"{y:04d}-{m:02d}-01T00:00:00Z")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    edges.reverse()  # oldest first
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def enumerate_markets(months: int, min_volume: float, cache_dir: str) -> List[dict]:
    """All closed markets with volume >= min_volume ending in the window."""
    out: Dict[str, dict] = {}
    for start, end in month_windows(months):

        def sweep(start=start, end=end):
            rows, offset = [], 0
            while True:
                page = _gget(
                    f"{GAMMA_API}/markets",
                    {
                        "closed": "true",
                        "volume_num_min": min_volume,
                        "end_date_min": start,
                        "end_date_max": end,
                        "limit": 100,
                        "offset": offset,
                    },
                )
                rows.extend(page or [])
                if not page or len(page) < 100 or offset >= 9_900:
                    if page and len(page) == 100:
                        print(f"[deep] WARNING: window {start} hit the offset cap")
                    return rows
                offset += 100

        key = f"deep/markets/{start[:7]}_{int(min_volume)}.json"
        for m in _cached(cache_dir, key, sweep):
            if m.get("conditionId"):
                out[m["conditionId"]] = m
        print(f"[deep] {start[:7]}: cumulative {len(out)} markets")
    return list(out.values())


def resolution_map(markets: List[dict]) -> Dict[str, dict]:
    """conditionId -> resolution record, resolved markets only."""
    res = {}
    for m in markets:
        if m.get("umaResolutionStatus") != "resolved":
            continue
        try:
            prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        except (ValueError, TypeError):
            continue
        if not prices or max(prices) < 0.999:
            continue
        res[m["conditionId"]] = {
            "question": m.get("question"),
            "closed": True,
            "resolved": True,
            "prices": prices,
            "end_ts": _parse_ts(m.get("closedTime") or m.get("endDate")),
            "tokens": m.get("clobTokenIds"),
        }
    return res


def _parse_ts(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    iso = iso.replace("Z", "").replace("+00", "").split("+")[0].strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(iso.split(".")[0], fmt.split(".")[0])))
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 2. Classify markets (structural first, event tags for the remainder)
# ---------------------------------------------------------------------------


def classify_markets(markets: List[dict], cache_dir: str, workers: int = 8) -> Dict[str, dict]:
    """conditionId -> {'category', 'insider'} using the scanner's rules."""
    out: Dict[str, dict] = {}
    need_event: List[dict] = []
    for m in markets:
        cond = m["conditionId"]
        if m.get("sportsMarketType") or m.get("gameStartTime"):
            out[cond] = {"category": "sports", "insider": False}
        elif PRICE_PATTERN.search(m.get("question") or ""):
            out[cond] = {"category": "price-market", "insider": False}
        else:
            need_event.append(m)

    def event_slug(m):
        evs = m.get("events") or []
        return (evs[0].get("slug") if evs else None) or m.get("slug") or ""

    slugs = sorted({event_slug(m) for m in need_event if event_slug(m)})
    print(
        f"[deep] structural: {len(out)} classified; fetching {len(slugs)} events "
        f"for {len(need_event)} markets"
    )

    def fetch(slug):
        try:
            return _cached(
                cache_dir,
                f"deep/events/{slug[:80]}.json",
                lambda: (_gget(f"{GAMMA_API}/events", {"slug": slug}) or [None])[0],
            )
        except Exception:
            return None

    events: Dict[str, Optional[dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for slug, ev in zip(slugs, pool.map(fetch, slugs)):
            events[slug] = ev

    for m in need_event:
        mc = classify_event(events.get(event_slug(m)), m.get("question") or "")
        out[m["conditionId"]] = {"category": mc.category, "insider": mc.insider_plausible}
    return out


def select_for_sweep(conds: List[str], classes: Dict[str, dict]) -> Tuple[List[str], List[str]]:
    """(insider-plausible markets, control sample of the rest)."""
    insiders, control = [], []
    for c in conds:
        if classes.get(c, {}).get("insider"):
            insiders.append(c)
        elif int(hashlib.md5(c.encode()).hexdigest()[:8], 16) % CONTROL_SAMPLE_MOD == 0:
            control.append(c)
    return insiders, control


# ---------------------------------------------------------------------------
# 3. Per-market tape sweeps
# ---------------------------------------------------------------------------


def sweep_trades(cond: str, min_cash: float, cache_dir: str) -> List[dict]:
    def sweep():
        fills, offset = [], 0
        while True:
            page = _gget(
                f"{DATA_API}/trades",
                {
                    "market": cond,
                    "limit": 100,
                    "offset": offset,
                    "takerOnly": "true",
                    "filterType": "CASH",
                    "filterAmount": min_cash,
                },
            )
            fills.extend(page or [])
            if not page or len(page) < 100 or len(fills) >= MAX_FILLS_PER_MARKET:
                return fills
            offset += 100

    try:
        return _cached(cache_dir, f"deep/trades/{cond[2:20]}.json", sweep)
    except Exception as ex:
        print(f"[deep] tape sweep failed for {cond[:18]}: {ex}")
        return []


def sweep_all(conds: List[str], min_cash: float, cache_dir: str, workers: int = 8) -> List[dict]:
    fills: List[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rows in pool.map(lambda c: sweep_trades(c, min_cash, cache_dir), conds):
            fills.extend(rows)
            done += 1
            if done % 250 == 0:
                print(f"[deep] swept {done}/{len(conds)} markets, {len(fills)} fills")
    return fills


# ---------------------------------------------------------------------------
# 4. Exit-strategy simulation
# ---------------------------------------------------------------------------

STRATEGIES: Dict[str, dict] = {
    "hold": {},
    "tp95": {"tp_abs": 0.95},
    "tp+10c": {"tp_plus": 0.10},
    "tp+20c": {"tp_plus": 0.20},
    "sl-15c": {"sl_minus": 0.15},
    "tp95/sl-15c": {"tp_abs": 0.95, "sl_minus": 0.15},
    "tp+20c/sl-20c": {"tp_plus": 0.20, "sl_minus": 0.20},
    "time-7d": {"max_days": 7},
    "time-30d": {"max_days": 30},
    "insider-exit": {"insider_exit": True},
}


def fetch_history(token: str, cache_dir: str) -> List[dict]:
    def fetch():
        return (
            _gget(
                f"{CLOB_API}/prices-history",
                {"market": token, "interval": "max", "fidelity": 720},
            )
            or {}
        ).get("history", [])

    try:
        return _cached(cache_dir, f"deep/prices/{token[:24]}.json", fetch)
    except Exception:
        return []


def insider_sell_ts(activity: Optional[List[dict]], cond: str, after_ts: int) -> Optional[int]:
    for a in activity or []:
        if (
            a.get("type") == "TRADE"
            and a.get("side") == "SELL"
            and a.get("conditionId") == cond
            and int(a.get("timestamp") or 0) > after_ts
        ):
            return int(a["timestamp"])
    return None


def simulate_exit(
    cand: Candidate,
    bars: List[dict],
    payout: float,
    end_ts: Optional[int],
    spec: dict,
    sell_ts: Optional[int],
) -> Tuple[float, float, str]:
    """Returns (return per $1, days held, how it exited)."""
    entry = min(cand.avg_price + SLIPPAGE, 0.999)
    t0 = cand.eval_ts
    tp = spec.get("tp_abs")
    if spec.get("tp_plus") is not None:
        tp = min(entry + spec["tp_plus"], 0.99)
    sl = entry - spec["sl_minus"] if spec.get("sl_minus") is not None else None
    deadline = t0 + spec["max_days"] * 86400 if spec.get("max_days") else None
    follow = spec.get("insider_exit") and sell_ts

    for bar in bars:
        t, p = int(bar["t"]), float(bar["p"])
        if t <= t0 or (end_ts and t > end_ts):
            continue
        if sl is not None and p <= sl:
            return (max(min(p, sl) - SLIPPAGE, 0.0) / entry) - 1.0, (t - t0) / 86400, "stop"
        if follow and t >= sell_ts:
            return (max(p - SLIPPAGE, 0.0) / entry) - 1.0, (t - t0) / 86400, "insider-sold"
        if tp is not None and p >= tp:
            return (tp / entry) - 1.0, (t - t0) / 86400, "take-profit"
        if deadline and t >= deadline:
            return (max(p - SLIPPAGE, 0.0) / entry) - 1.0, (t - t0) / 86400, "time"
    held_days = ((end_ts or (int(bars[-1]["t"]) if bars else t0)) - t0) / 86400
    return (payout / entry) - 1.0, max(held_days, 0.0), "resolution"


def run_exit_sims(
    alerts: List[Candidate],
    resolutions: Dict[str, dict],
    activities: Dict[str, list],
    cache_dir: str,
) -> Dict[str, dict]:
    rows: Dict[str, dict] = {}
    inputs = []
    skipped = 0
    for c in alerts:
        res = resolutions.get(c.condition_id)
        if not res or c.outcome_index >= len(res["prices"]):
            continue
        bars = fetch_history(c.asset, cache_dir)
        if not bars:
            skipped += 1
            continue
        sell = insider_sell_ts(activities.get(c.wallet), c.condition_id, c.eval_ts)
        inputs.append((c, bars, res["prices"][c.outcome_index], res.get("end_ts"), sell))
    if skipped:
        print(f"[deep] exit sim: {skipped} alerts skipped (no price history)")

    for name, spec in STRATEGIES.items():
        rets, days, hows = [], [], defaultdict(int)
        for c, bars, payout, end_ts, sell in inputs:
            r, d, how = simulate_exit(c, bars, payout, end_ts, spec, sell)
            rets.append(r)
            days.append(d)
            hows[how] += 1
        n = len(rets)
        srt = sorted(rets)
        rows[name] = {
            "n": n,
            "avg_ret": sum(rets) / n if n else None,
            "median_ret": srt[n // 2] if n else None,
            "win_rate": sum(1 for r in rets if r > 0) / n if n else None,
            "avg_days": sum(days) / n if n else None,
            "exits": dict(hows),
        }
    return rows


def format_exits(rows: Dict[str, dict]) -> str:
    head = f"{'strategy':16} {'n':>4} {'avg ret/$':>10} {'median':>8} {'win%':>6} {'avg days':>9}  exit mix"
    lines = [head, "-" * len(head)]
    for name, s in rows.items():
        if not s["n"]:
            continue
        mix = ", ".join(f"{k}:{v}" for k, v in sorted(s["exits"].items(), key=lambda kv: -kv[1]))
        lines.append(
            f"{name:16} {s['n']:>4} {s['avg_ret']:>+10.3f} {s['median_ret']:>+8.3f} "
            f"{100*s['win_rate']:>5.1f}% {s['avg_days']:>9.1f}  {mix}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Year-scale resolved-only backtest with exit sims")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--min-cash", type=float, default=25_000.0)
    p.add_argument("--min-volume", type=float, default=200_000.0)
    p.add_argument("--alert-score", type=int, default=60)
    p.add_argument("--cache-dir", default="copy_trading/backtest_cache")
    p.add_argument("--json", default=None)
    args = p.parse_args(argv)

    cfg = CopyConfig(min_trade_cash=args.min_cash, alert_score=args.alert_score)

    print(
        f"[deep] enumerating {args.months} months of closed markets "
        f"(volume >= ${args.min_volume:,.0f})..."
    )
    markets = enumerate_markets(args.months, args.min_volume, args.cache_dir)
    resolutions = resolution_map(markets)
    print(f"[deep] {len(markets)} markets, {len(resolutions)} cleanly resolved")

    classes = classify_markets(
        [m for m in markets if m["conditionId"] in resolutions], args.cache_dir
    )
    insider_conds, control_conds = select_for_sweep(sorted(resolutions), classes)
    print(
        f"[deep] insider-plausible: {len(insider_conds)}; "
        f"control sample (1/{CONTROL_SAMPLE_MOD} of the rest): {len(control_conds)}"
    )

    fills = sweep_all(insider_conds + control_conds, args.min_cash, args.cache_dir)
    fills.sort(key=lambda t: int(t["timestamp"]))
    if not fills:
        print("[deep] no fills found")
        return 1
    t0, t1 = int(fills[0]["timestamp"]), int(fills[-1]["timestamp"])
    wallets = sorted({t["proxyWallet"].lower() for t in fills})
    print(
        f"[deep] {len(fills)} fills {time.strftime('%Y-%m-%d', time.gmtime(t0))} -> "
        f"{time.strftime('%Y-%m-%d', time.gmtime(t1))}; profiling {len(wallets)} wallets..."
    )
    activities = fetch_activities(wallets, args.cache_dir, workers=8)

    candidates = replay(fills, activities, cfg)
    for c in candidates:
        cl = classes.get(c.condition_id, {})
        c.market_category = cl.get("category", "unclear")
        c.insider_plausible = bool(cl.get("insider"))
    evaluate(candidates, resolutions, SLIPPAGE)
    resolved = [c for c in candidates if c.market_resolved and c.ret_per_dollar is not None]
    print(f"[deep] {len(candidates)} candidate buckets, {len(resolved)} resolved buys\n")

    print(
        f"=== DEEP BACKTEST {time.strftime('%Y-%m-%d', time.gmtime(t0))} -> "
        f"{time.strftime('%Y-%m-%d', time.gmtime(t1))} (resolved only, "
        f"entry = insider price + {SLIPPAGE:.2f}) ===\n"
    )
    ins = [c for c in resolved if c.insider_plausible]
    ctl = [c for c in resolved if not c.insider_plausible]
    groups = {
        "INSIDER MARKETS: all big buys": ins,
        f"  insider alerts (score>={cfg.alert_score})": [
            c for c in ins if c.score >= cfg.alert_score
        ],
        "  insider alerts score>=75": [c for c in ins if c.score >= 75],
        "  insider alerts score 90-100": [c for c in ins if c.score >= 90],
        "  insider non-alert (score<60)": [c for c in ins if c.score < 60],
        f"CONTROL sample (1/{CONTROL_SAMPLE_MOD} sports/other)": ctl,
        "  control alerts (score>=60)": [c for c in ctl if c.score >= 60],
        "  fresh<3d anywhere": [
            c
            for c in resolved
            if c.wallet_age_days is not None and c.wallet_age_days <= 3 and not c.profile_capped
        ],
    }
    print(format_table({k: _stats(v) for k, v in groups.items()}))

    ins_alerts = [c for c in ins if c.score >= cfg.alert_score]
    ins_alerts.sort(key=lambda c: -c.final_cash)
    print(f"\nTop insider alerts by bet size ({min(20, len(ins_alerts))} of {len(ins_alerts)}):")
    for c in ins_alerts[:20]:
        when = time.strftime("%y-%m-%d", time.gmtime(c.eval_ts))
        print(
            f"  [{when}] {c.name or c.wallet[:10]:16.16} BUY {c.outcome:12.12} "
            f"@ {c.avg_price:.2f} ${c.final_cash:>9,.0f} score={c.score:<3} "
            f"{'WON ' if c.won else 'LOST'} {c.ret_per_dollar:>+7.1%} | {c.title[:42]}"
        )

    print("\n=== EXIT STRATEGIES (insider alerts, score>=60) ===")
    print(format_exits(run_exit_sims(ins_alerts, resolutions, activities, args.cache_dir)))
    ins75 = [c for c in ins_alerts if c.score >= 75]
    print("\n=== EXIT STRATEGIES (insider alerts, score>=75) ===")
    print(format_exits(run_exit_sims(ins75, resolutions, activities, args.cache_dir)))

    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(c) for c in candidates], f, indent=1)
        print(f"\n[deep] wrote {len(candidates)} candidates to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
