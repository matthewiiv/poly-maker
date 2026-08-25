"""
Turns a whale signal into a copy order.

Dry-run by default: the executor prints exactly what it would do and tracks
a simulated position in the state file. In live mode it places real orders
through the same PolymarketClient the market maker uses.

Copy mechanics:
- BUY signals are mirrored with cash = whale_cash * copy_ratio, clamped by
  per-trade / per-market / total caps, as a marketable limit order priced at
  the whale's average fill plus a slippage allowance. If the book has already
  run away past that allowance we skip instead of chasing.
- SELL signals from watched wallets close whatever we copied on that token
  (the whale is leaving; follow them out). We never short.
"""

import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from copy_trading.config import CopyConfig
from copy_trading.data_api import best_prices, get_book
from copy_trading.signals import WhaleSignal
from copy_trading.state import StateStore


def round_to_tick(price: float, tick: float, side: str) -> float:
    """
    Snap a price to the market's tick grid, rounding conservatively:
    down for BUY (never pay above the cap), up for SELL (never undercut it).
    Result is clamped inside (0, 1) by one tick.
    """
    steps = price / tick
    snapped = (math.floor(steps) if side == "BUY" else math.ceil(steps)) * tick
    snapped = max(tick, min(1.0 - tick, snapped))
    return round(snapped, 6)


def plan_buy_price(
    whale_price: float, best_ask: Optional[float], tick: float, max_slippage: float
) -> Optional[float]:
    """
    Limit price for a copy BUY, or None if the market already moved more than
    max_slippage past the whale's price (don't chase).
    """
    cap = whale_price + max_slippage
    if best_ask is not None and best_ask > cap + 1e-9:
        return None
    return round_to_tick(cap, tick, "BUY")


def plan_sell_price(
    whale_price: float, best_bid: Optional[float], tick: float, max_slippage: float
) -> Optional[float]:
    """
    Limit price for a copy SELL, or None if the bid already collapsed more
    than max_slippage below the whale's exit price.
    """
    floor = whale_price - max_slippage
    if best_bid is None or best_bid < floor - 1e-9:
        return None
    return round_to_tick(max(floor, tick), tick, "SELL")


def size_copy_cash(
    whale_cash: float, cfg: CopyConfig, market_spent: float, total_spent: float
) -> float:
    """
    Cash to commit to a copy BUY after applying the ratio and all caps.
    Returns 0.0 when the remaining budget is below min_copy_cash.
    """
    cash = whale_cash * cfg.copy_ratio
    cash = min(
        cash,
        cfg.max_per_trade_usdc,
        cfg.max_per_market_usdc - market_spent,
        cfg.max_total_usdc - total_spent,
    )
    return cash if cash >= cfg.min_copy_cash else 0.0


@dataclass
class CopyPlan:
    asset: str
    side: str
    price: float
    shares: float
    cash: float
    neg_risk: bool
    note: str = ""


class CopyExecutor:
    """
    Plans and (optionally) executes copy orders.

    Args:
        cfg: copy-trading configuration
        state: persistent state (exposure caps, simulated holdings)
        live: place real orders when True; otherwise dry-run
        book_fn: injectable order-book fetcher (tests stub this)
    """

    def __init__(
        self,
        cfg: CopyConfig,
        state: StateStore,
        live: bool = False,
        book_fn: Callable[[str], dict] = get_book,
    ):
        self.cfg = cfg
        self.state = state
        self.live = live
        self.book_fn = book_fn
        self._client = None

    def _ensure_client(self):
        # Imported lazily so scan/dry-run modes never need PK/BROWSER_ADDRESS.
        if self._client is None:
            from poly_data.polymarket_client import PolymarketClient

            self._client = PolymarketClient()
        return self._client

    # -- planning -----------------------------------------------------------
    def plan(self, signal: WhaleSignal) -> Optional[CopyPlan]:
        if signal.side == "BUY":
            return self._plan_buy(signal)
        return self._plan_sell(signal)

    def _fetch_book(self, signal: WhaleSignal) -> Optional[dict]:
        # A 404 means the market is closed/resolved (e.g. a whale print from a
        # game that already ended) — nothing left to copy.
        try:
            return self.book_fn(signal.asset)
        except requests.HTTPError as ex:
            if ex.response is not None and ex.response.status_code == 404:
                self._log(signal, "skip: order book gone (market closed or resolved)")
                return None
            raise

    def _plan_buy(self, signal: WhaleSignal) -> Optional[CopyPlan]:
        cash = size_copy_cash(
            signal.total_cash,
            self.cfg,
            self.state.market_spent(signal.condition_id),
            self.state.total_spent,
        )
        if cash <= 0:
            self._log(signal, "skip buy: exposure caps reached (or copy size below minimum)")
            return None

        book = self._fetch_book(signal)
        if book is None:
            return None
        tick = float(book.get("tick_size") or 0.01)
        min_shares = float(book.get("min_order_size") or 5)
        neg_risk = bool(book.get("neg_risk", False))
        _, best_ask = best_prices(book)

        price = plan_buy_price(signal.avg_price, best_ask, tick, self.cfg.max_slippage)
        if price is None:
            self._log(
                signal,
                f"skip buy: ask {best_ask} moved more than {self.cfg.max_slippage} "
                f"past whale price {signal.avg_price:.3f}",
            )
            return None

        shares = round(cash / price, 2)
        if shares < min_shares:
            # Bump to the exchange minimum if the caps allow it, else skip.
            bumped_cash = min_shares * price
            remaining = min(
                self.cfg.max_per_trade_usdc,
                self.cfg.max_per_market_usdc - self.state.market_spent(signal.condition_id),
                self.cfg.max_total_usdc - self.state.total_spent,
            )
            if bumped_cash > remaining + 1e-9:
                self._log(signal, f"skip buy: {shares} shares below exchange minimum {min_shares}")
                return None
            shares, cash = min_shares, round(bumped_cash, 2)

        return CopyPlan(
            asset=signal.asset,
            side="BUY",
            price=price,
            shares=shares,
            cash=round(shares * price, 2),
            neg_risk=neg_risk,
            note=f"mirror {signal.side} of ${signal.total_cash:,.0f} by {signal.wallet}",
        )

    def _plan_sell(self, signal: WhaleSignal) -> Optional[CopyPlan]:
        held = self.state.holdings.get(signal.asset)
        if not held or held["shares"] <= 0:
            self._log(signal, "skip sell: no copied position on this token")
            return None

        book = self._fetch_book(signal)
        if book is None:
            return None
        tick = float(book.get("tick_size") or 0.01)
        min_shares = float(book.get("min_order_size") or 5)
        neg_risk = bool(book.get("neg_risk", False))
        best_bid, _ = best_prices(book)

        price = plan_sell_price(signal.avg_price, best_bid, tick, self.cfg.max_slippage)
        if price is None:
            self._log(
                signal,
                f"skip sell: bid {best_bid} collapsed more than {self.cfg.max_slippage} "
                f"below whale exit {signal.avg_price:.3f}",
            )
            return None

        shares = round(held["shares"], 2)
        if shares < min_shares:
            self._log(signal, f"skip sell: held {shares} below exchange minimum {min_shares}")
            return None

        return CopyPlan(
            asset=signal.asset,
            side="SELL",
            price=price,
            shares=shares,
            cash=round(shares * price, 2),
            neg_risk=neg_risk,
            note=f"whale {signal.wallet} sold ${signal.total_cash:,.0f}; exiting copy",
        )

    # -- execution ----------------------------------------------------------
    def execute(self, signal: WhaleSignal, plan: CopyPlan) -> bool:
        mode = "LIVE" if self.live else "DRY-RUN"
        print(
            f"[{_now()}] [{mode}] {plan.side} {plan.shares} shares of "
            f"'{signal.outcome}' @ {plan.price} (${plan.cash}) in '{signal.title}' "
            f"| {plan.note}"
        )

        if self.live:
            client = self._ensure_client()
            resp = client.create_order(
                plan.asset, plan.side, plan.price, plan.shares, neg_risk=plan.neg_risk
            )
            if not resp:
                print(f"[{_now()}] order rejected/failed; not recording exposure")
                return False
            print(f"[{_now()}] order response: {resp}")

        if plan.side == "BUY":
            self.state.record_buy(
                plan.asset,
                signal.condition_id,
                signal.title,
                signal.outcome,
                plan.shares,
                plan.cash,
            )
        else:
            self.state.record_sell(plan.asset, plan.shares, plan.cash)
        self.state.save()
        return True

    def _log(self, signal: WhaleSignal, msg: str) -> None:
        print(f"[{_now()}] [{signal.title[:50]}] {msg}")


def _now() -> str:
    return time.strftime("%H:%M:%S")
