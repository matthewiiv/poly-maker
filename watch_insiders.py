"""
Watch Polymarket for "fresh wallet suddenly bets big" insiders — and
optionally copy-trade them.

Modes (least to most dangerous):

    # 1. Alert-only: print/webhook insider alerts, place no orders
    uv run python watch_insiders.py

    # 2. Dry-run copying: also print the exact orders it WOULD place,
    #    tracking a simulated portfolio in copy_trading/state.json
    uv run python watch_insiders.py --copy

    # 3. Live copying: real orders via your PK/BROWSER_ADDRESS credentials.
    #    Requires BOTH the --live flag and COPY_TRADER_LIVE=YES in the env.
    uv run python watch_insiders.py --copy --live

Follow specific wallets (e.g. one you read about in the news):

    uv run python watch_insiders.py --copy --wallets 0x19c3b385be5667154fc69c87d8f7914be84087c1

Single diagnostic pass over the recent tape:

    uv run python watch_insiders.py --once --lookback 86400
"""

import argparse
import os
import sys

from dotenv import load_dotenv

from copy_trading import CopyConfig, CopyExecutor, StateStore, InsiderScanner
from copy_trading.scanner import DEFAULT_MAX_FILL_AGE_SECS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Polymarket insider watcher / copy trader",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    d = CopyConfig()

    det = p.add_argument_group("detection")
    det.add_argument(
        "--min-cash",
        type=float,
        default=d.min_trade_cash,
        help="min USDC value of a tape fill to consider",
    )
    det.add_argument(
        "--alert-score",
        type=int,
        default=d.alert_score,
        help="signal score (0-100) required to alert/copy",
    )
    det.add_argument(
        "--fresh-days",
        type=float,
        default=d.fresh_wallet_days,
        help="wallet age (days) that counts as brand new",
    )
    det.add_argument(
        "--lookback",
        type=float,
        default=DEFAULT_MAX_FILL_AGE_SECS,
        help="ignore tape fills older than this many seconds",
    )
    det.add_argument(
        "--all-markets",
        action="store_true",
        help="alert on every market (default: only insider-plausible ones — "
        "sports, esports and price-level markets are filtered out)",
    )

    cp = p.add_argument_group("copying")
    cp.add_argument("--copy", action="store_true", help="plan copy orders (dry-run unless --live)")
    cp.add_argument(
        "--live",
        action="store_true",
        help="place REAL orders (also needs COPY_TRADER_LIVE=YES in env)",
    )
    cp.add_argument(
        "--ratio",
        type=float,
        default=d.copy_ratio,
        help="fraction of the insider's cash size to mirror",
    )
    cp.add_argument(
        "--max-per-trade", type=float, default=d.max_per_trade_usdc, help="max USDC per copy order"
    )
    cp.add_argument(
        "--max-per-market",
        type=float,
        default=d.max_per_market_usdc,
        help="max USDC committed per market",
    )
    cp.add_argument(
        "--max-total",
        type=float,
        default=d.max_total_usdc,
        help="max USDC committed across all copies",
    )
    cp.add_argument(
        "--slippage",
        type=float,
        default=d.max_slippage,
        help="max price move (probability points) past the insider's fill",
    )
    cp.add_argument(
        "--follow-min-cash",
        type=float,
        default=d.follow_min_cash,
        help="min USDC fill from a watched wallet to mirror",
    )

    ops = p.add_argument_group("operations")
    ops.add_argument(
        "--wallets", default="", help="comma-separated wallet addresses to watch from the start"
    )
    ops.add_argument("--poll", type=float, default=d.poll_secs, help="seconds between tape polls")
    ops.add_argument(
        "--state",
        default=None,
        help="state file path (default: copy_trading/state.json, "
        "or state.live.json in live mode)",
    )
    ops.add_argument(
        "--webhook", default=None, help="alert webhook URL (default: env COPY_TRADER_WEBHOOK_URL)"
    )
    ops.add_argument("--once", action="store_true", help="run a single poll pass and exit")
    return p


def main(argv=None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    if args.live and not args.copy:
        print("--live only makes sense together with --copy")
        return 2
    if args.live:
        if os.getenv("COPY_TRADER_LIVE") != "YES":
            print(
                "Refusing to trade live: set COPY_TRADER_LIVE=YES in your environment "
                "to confirm you understand this places real orders with real money."
            )
            return 2
        if not os.getenv("PK") or not os.getenv("BROWSER_ADDRESS"):
            print("Live mode needs PK and BROWSER_ADDRESS in the environment (see .env.example).")
            return 2

    cfg = CopyConfig(
        min_trade_cash=args.min_cash,
        alert_score=args.alert_score,
        fresh_wallet_days=args.fresh_days,
        insider_only=not args.all_markets,
        copy_ratio=args.ratio,
        max_per_trade_usdc=args.max_per_trade,
        max_per_market_usdc=args.max_per_market,
        max_total_usdc=args.max_total,
        max_slippage=args.slippage,
        follow_min_cash=args.follow_min_cash,
        poll_secs=args.poll,
        webhook_url=args.webhook or os.getenv("COPY_TRADER_WEBHOOK_URL"),
    )

    state_path = args.state or (
        "copy_trading/state.live.json" if args.live else "copy_trading/state.json"
    )
    state = StateStore(state_path)

    executor = CopyExecutor(cfg, state, live=args.live) if args.copy else None
    scanner = InsiderScanner(cfg, state, executor, max_fill_age_secs=args.lookback)

    if args.wallets:
        scanner.seed_watchlist(args.wallets.split(","))

    if args.once:
        n = scanner.poll_once()
        print(
            f"[scanner] single pass done: {n} new fill(s) processed, "
            f"{len(scanner.buckets)} bucket(s), state saved to {state_path}"
        )
        state.save()
        return 0

    scanner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
