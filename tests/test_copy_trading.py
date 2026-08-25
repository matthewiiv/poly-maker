"""
Tests for the copy_trading package. Pure logic only — no network calls;
API access is stubbed via the injectable *_fn hooks.
"""

import time

import pytest
import requests

from copy_trading.config import CopyConfig
from copy_trading.executor import (
    CopyExecutor,
    plan_buy_price,
    plan_sell_price,
    round_to_tick,
    size_copy_cash,
)
from copy_trading.market_class import MarketClass, classify_event
from copy_trading.scanner import InsiderScanner
from copy_trading.signals import WalletProfile, profile_wallet, score_insider
from copy_trading.state import StateStore


class StubClassifier:
    def __init__(self, mc):
        self.mc = mc

    def classify(self, slug, title=""):
        return self.mc


INSIDER_CLASS = MarketClass("insider", True, ["tag:test"])
SPORTS_CLASS = MarketClass("sports", False, ["tag:sports"])

# Recent epoch: StateStore prunes seen/alerted entries against wall-clock
# time, so fabricated timestamps must be near the present.
NOW = time.time()
WALLET = "0x19c3b385be5667154fc69c87d8f7914be84087c1"


def make_trade(ts=NOW, size=100_000.0, price=0.85, side="BUY", tx="0xabc", wallet=WALLET):
    return {
        "proxyWallet": wallet,
        "side": side,
        "asset": "123456789",
        "conditionId": "0xcond",
        "size": size,
        "price": price,
        "timestamp": int(ts),
        "title": "Clarity Act signed into law in 2026?",
        "slug": "clarity-act",
        "eventSlug": "clarity-act",
        "outcome": "No",
        "outcomeIndex": 1,
        "name": "insider",
        "transactionHash": tx,
    }


def make_book(best_bid=0.84, best_ask=0.86, tick=0.01, min_size=5, neg_risk=False):
    return {
        "bids": [
            {"price": str(best_bid - 0.02), "size": "1000"},
            {"price": str(best_bid), "size": "1000"},
        ],
        "asks": [
            {"price": str(best_ask + 0.02), "size": "1000"},
            {"price": str(best_ask), "size": "1000"},
        ],
        "tick_size": tick,
        "min_order_size": min_size,
        "neg_risk": neg_risk,
    }


def fresh_profile(age_days=0.3, trades=3):
    return WalletProfile(
        wallet=WALLET,
        first_seen_ts=int(NOW - age_days * 86400),
        activity_count=trades,
        trade_count=trades,
        total_cash_traded=350_000.0,
        markets_traded=1,
        capped=False,
    )


def veteran_profile():
    return WalletProfile(
        wallet=WALLET,
        first_seen_ts=int(NOW - 400 * 86400),
        activity_count=500,
        trade_count=480,
        total_cash_traded=5_000_000.0,
        markets_traded=120,
        capped=True,
    )


# -- profiling / scoring ----------------------------------------------------


def test_profile_wallet_from_activity():
    activity = [
        {"type": "TRADE", "timestamp": int(NOW - 7200), "usdcSize": 104_000, "conditionId": "0xa"},
        {"type": "TRADE", "timestamp": int(NOW - 7100), "usdcSize": 222_000, "conditionId": "0xa"},
        {"type": "TRADE", "timestamp": int(NOW - 7000), "usdcSize": 26_000, "conditionId": "0xa"},
    ]
    p = profile_wallet(WALLET, activity=activity)
    assert p.trade_count == 3
    assert p.markets_traded == 1
    assert not p.capped
    assert p.total_cash_traded == pytest.approx(352_000)
    assert p.age_days(NOW) == pytest.approx(7200 / 86400, abs=1e-4)


def test_fresh_insider_scores_above_default_threshold():
    cfg = CopyConfig()
    score, reasons = score_insider(350_000, 0.85, "BUY", fresh_profile(), cfg, NOW)
    assert score >= cfg.alert_score
    assert any(r.startswith("fresh-wallet") for r in reasons)
    assert any(r.startswith("big-bet") for r in reasons)


def test_veteran_wallet_scores_below_threshold():
    cfg = CopyConfig()
    score, _ = score_insider(60_000, 0.85, "BUY", veteran_profile(), cfg, NOW)
    # A big bet alone (no freshness signals) should not trip the alert.
    assert score < cfg.alert_score


# -- pricing / sizing -------------------------------------------------------


def test_round_to_tick_directional():
    assert round_to_tick(0.876, 0.01, "BUY") == pytest.approx(0.87)
    assert round_to_tick(0.871, 0.01, "SELL") == pytest.approx(0.88)
    # clamped inside the (0, 1) range by one tick
    assert round_to_tick(0.0001, 0.01, "BUY") == pytest.approx(0.01)
    assert round_to_tick(1.5, 0.01, "SELL") == pytest.approx(0.99)


def test_plan_buy_price_caps_and_skips():
    # insider at 0.85, ask at 0.86, 3c allowance -> pay up to 0.88
    assert plan_buy_price(0.85, 0.86, 0.01, 0.03) == pytest.approx(0.88)
    # ask ran to 0.90 -> don't chase
    assert plan_buy_price(0.85, 0.90, 0.01, 0.03) is None


def test_plan_sell_price_floor_and_skips():
    assert plan_sell_price(0.85, 0.84, 0.01, 0.03) == pytest.approx(0.82)
    # bid collapsed below allowance -> skip
    assert plan_sell_price(0.85, 0.70, 0.01, 0.03) is None


def test_size_copy_cash_applies_ratio_and_caps():
    cfg = CopyConfig(
        copy_ratio=0.001, max_per_trade_usdc=100, max_per_market_usdc=250, max_total_usdc=1000
    )
    assert size_copy_cash(50_000, cfg, 0, 0) == pytest.approx(50.0)
    assert size_copy_cash(500_000, cfg, 0, 0) == pytest.approx(100.0)  # per-trade cap
    assert size_copy_cash(500_000, cfg, 200, 0) == pytest.approx(50.0)  # market cap
    assert size_copy_cash(500_000, cfg, 0, 950) == pytest.approx(50.0)  # total cap
    assert size_copy_cash(500_000, cfg, 0, 999.5) == 0.0  # below min copy


# -- state ------------------------------------------------------------------


def test_state_round_trip(tmp_path):
    path = str(tmp_path / "state.json")
    s = StateStore(path)
    assert s.mark_seen("fill1", NOW)
    assert not s.mark_seen("fill1", NOW)
    s.add_to_watchlist(WALLET, "test")
    s.record_buy("asset1", "0xcond", "title", "No", 100.0, 85.0)
    s.save()

    s2 = StateStore(path)
    assert not s2.mark_seen("fill1", NOW)
    assert s2.is_watched(WALLET.upper())
    assert s2.total_spent == pytest.approx(85.0)
    assert s2.market_spent("0xcond") == pytest.approx(85.0)

    s2.record_sell("asset1", 100.0, 90.0)
    assert "asset1" not in s2.holdings
    assert s2.total_proceeds == pytest.approx(90.0)


# -- executor ---------------------------------------------------------------


def make_executor(tmp_path, book=None, **cfg_kwargs):
    cfg = CopyConfig(**cfg_kwargs)
    state = StateStore(str(tmp_path / "state.json"))
    ex = CopyExecutor(cfg, state, live=False, book_fn=lambda asset: book or make_book())
    return cfg, state, ex


def bucket_from(trade):
    from copy_trading.signals import InsiderSignal

    return InsiderSignal.from_trade(trade)


def test_executor_buy_plan_and_simulated_fill(tmp_path):
    _, state, ex = make_executor(tmp_path)
    sig = bucket_from(make_trade(size=100_000, price=0.85))  # $85k insider buy
    plan = ex.plan(sig)
    assert plan is not None and plan.side == "BUY"
    assert plan.price == pytest.approx(0.88)  # 0.85 + 0.03 slippage cap
    assert plan.cash <= 100.0 + 1e-6

    assert ex.execute(sig, plan)
    assert state.total_spent == pytest.approx(plan.cash)
    assert state.holdings[sig.asset]["shares"] == pytest.approx(plan.shares)


def test_executor_skips_when_market_ran_away(tmp_path):
    _, _, ex = make_executor(tmp_path, book=make_book(best_ask=0.95))
    plan = ex.plan(bucket_from(make_trade(price=0.85)))
    assert plan is None


def test_executor_skips_closed_markets(tmp_path):
    # /book returns 404 once a market resolves; the plan should just skip.
    def gone(asset):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError(response=resp)

    cfg = CopyConfig()
    state = StateStore(str(tmp_path / "state.json"))
    ex = CopyExecutor(cfg, state, live=False, book_fn=gone)
    assert ex.plan(bucket_from(make_trade())) is None


def test_executor_bumps_to_exchange_minimum(tmp_path):
    # ~$2 copy of a ~$2k follow trade -> under 5-share minimum, gets bumped
    _, _, ex = make_executor(tmp_path, copy_ratio=0.001)
    plan = ex.plan(bucket_from(make_trade(size=2_500, price=0.85)))  # $2,125 cash
    assert plan is not None
    assert plan.shares == pytest.approx(5)


def test_executor_sell_only_closes_held_copies(tmp_path):
    _, state, ex = make_executor(tmp_path)
    sell_sig = bucket_from(make_trade(side="SELL", price=0.85))
    assert ex.plan(sell_sig) is None  # nothing held -> no short

    state.record_buy("123456789", "0xcond", "t", "No", 50.0, 42.5)
    plan = ex.plan(sell_sig)
    assert plan is not None and plan.side == "SELL"
    assert plan.shares == pytest.approx(50.0)
    assert ex.execute(sell_sig, plan)
    assert "123456789" not in state.holdings


# -- scanner ----------------------------------------------------------------


def make_scanner(
    tmp_path, trades, profile, cfg=None, with_executor=True, wallet_trades=None, mclass=None
):
    cfg = cfg or CopyConfig()
    state = StateStore(str(tmp_path / "state.json"))
    executor = (
        CopyExecutor(cfg, state, live=False, book_fn=lambda asset: make_book())
        if with_executor
        else None
    )
    scanner = InsiderScanner(
        cfg,
        state,
        executor,
        profile_fn=lambda wallet: profile,
        trades_fn=lambda min_cash, limit=100: trades,
        wallet_trades_fn=lambda wallet, limit=25: wallet_trades or [],
        classifier=StubClassifier(mclass or INSIDER_CLASS),
    )
    return state, scanner


def test_scanner_aggregates_fills_alerts_once_and_copies(tmp_path):
    trades = [
        make_trade(ts=NOW - 10, size=50_000, price=0.85, tx="0x1"),
        make_trade(ts=NOW - 8, size=100_000, price=0.85, tx="0x2"),
    ]
    state, scanner = make_scanner(tmp_path, trades, fresh_profile())

    assert scanner.poll_once(NOW) == 2
    key = f"{WALLET}:123456789:BUY"
    assert scanner.buckets[key].total_cash == pytest.approx(150_000 * 0.85)
    assert state.is_watched(WALLET)  # auto-added
    assert len(state.alerted) == 1  # one alert for the bucket
    assert state.total_spent > 0  # copy executed (dry-run)

    spent = state.total_spent
    assert scanner.poll_once(NOW + 5) == 0  # same tape -> all deduped
    assert state.total_spent == spent  # no double copy


def test_scanner_ignores_stale_and_veteran_fills(tmp_path):
    stale = [make_trade(ts=NOW - 7200, tx="0xold")]
    state, scanner = make_scanner(tmp_path, stale, fresh_profile())
    scanner.poll_once(NOW)
    assert not state.alerted and not scanner.buckets

    veteran_trades = [make_trade(ts=NOW - 10, tx="0xvet")]
    state2, scanner2 = make_scanner(tmp_path / "v", veteran_trades, veteran_profile())
    scanner2.poll_once(NOW)
    assert not state2.alerted
    assert not state2.is_watched(WALLET)


def test_scanner_follows_watchlisted_wallet_small_trades(tmp_path):
    # $2,125 fill: far below the $25k tape filter, but caught via the watchlist
    follow = [make_trade(ts=NOW - 5, size=2_500, price=0.85, tx="0xf")]
    state, scanner = make_scanner(
        tmp_path, trades=[], profile=fresh_profile(), wallet_trades=follow
    )
    scanner.seed_watchlist([WALLET])
    scanner.poll_once(NOW)
    assert len(state.alerted) == 1  # follow event fired
    assert state.total_spent > 0  # and was copied


# -- backtest ---------------------------------------------------------------

from copy_trading.backtest import Candidate, evaluate, profile_at, replay  # noqa: E402


def test_profile_at_is_point_in_time():
    activity = [
        {"type": "TRADE", "timestamp": 1000, "usdcSize": 10, "conditionId": "a"},
        {"type": "TRADE", "timestamp": 2000, "usdcSize": 20, "conditionId": "b"},
    ]
    p = profile_at("0xw", activity, 1500)
    assert p.trade_count == 1 and p.first_seen_ts == 1000 and p.markets_traded == 1
    # before the wallet existed: no history at all
    p0 = profile_at("0xw", activity, 999)
    assert p0.first_seen_ts is None and p0.age_days(999) is None


def test_replay_evaluates_bucket_once_at_cash_crossing():
    fills = [
        make_trade(ts=NOW - 100, size=20_000, price=0.8, tx="0xa"),  # $16k: under min
        make_trade(ts=NOW - 90, size=20_000, price=0.8, tx="0xb"),  # $32k: crosses
    ]
    activity = [
        {"type": "TRADE", "timestamp": int(NOW - 100), "usdcSize": 16_000, "conditionId": "0xcond"},
        {"type": "TRADE", "timestamp": int(NOW - 90), "usdcSize": 16_000, "conditionId": "0xcond"},
    ]
    cands = replay(fills, {WALLET: activity}, CopyConfig())
    assert len(cands) == 1
    c = cands[0]
    assert c.cash_at_eval == pytest.approx(32_000)
    assert c.final_cash == pytest.approx(32_000)
    assert c.fills == 2
    # brand-new wallet crossing with a $32k conviction buy scores as an alert
    assert c.score >= CopyConfig().alert_score
    assert c.wallet_age_days is not None and c.wallet_age_days < 0.01


def test_evaluate_resolution_pnl():
    c = Candidate(
        wallet=WALLET,
        name="w",
        asset="1",
        condition_id="0xc",
        side="BUY",
        outcome="No",
        outcome_index=1,
        title="t",
        slug="s",
        eval_ts=int(NOW),
        avg_price=0.8,
        cash_at_eval=50_000,
    )
    markets = {"0xc": {"resolved": True, "closed": True, "question": "t", "prices": [0.0, 1.0]}}
    evaluate([c], markets, slippage_penalty=0.01)
    assert c.won is True
    assert c.entry_price == pytest.approx(0.81)
    assert c.ret_per_dollar == pytest.approx(1 / 0.81 - 1)


# -- market classifier ------------------------------------------------------


def test_classify_event_structural_sports_and_deny_tags():
    ev = {"tags": [{"slug": "crypto"}], "markets": [{"sportsMarketType": "moneyline"}]}
    assert classify_event(ev, "Arsenal vs City").insider_plausible is False

    ev2 = {"tags": [{"slug": "sports"}, {"slug": "epl"}], "markets": []}
    mc = classify_event(ev2, "Will Chelsea FC win on 2026-08-24?")
    assert mc.category == "sports" and not mc.insider_plausible


def test_classify_event_price_markets_and_recurring():
    ev = {"tags": [{"slug": "crypto-prices"}, {"slug": "bitcoin"}]}
    assert classify_event(ev, "Will Bitcoin dip to $45,000?").category == "price-market"

    ev2 = {"tags": [{"slug": "weather"}], "series": [{"recurrence": "daily"}]}
    assert classify_event(ev2, "Highest temperature in NYC today?").insider_plausible is False


def test_classify_event_insider_paths():
    # Tag-based: the CLARITY Act pattern
    ev = {"tags": [{"slug": "politics"}, {"slug": "us-law"}, {"slug": "crypto"}]}
    mc = classify_event(ev, "Clarity Act (H.R.3633) signed into law in 2026?")
    assert mc.category == "insider" and mc.insider_plausible

    # Verb-based fallback: exchange listing with only a generic tag
    mc2 = classify_event({"tags": [{"slug": "crypto"}]}, "Will Coinbase list PUMP in August?")
    assert mc2.insider_plausible and any(r.startswith("verb:") for r in mc2.reasons)

    # No signal at all
    mc3 = classify_event({"tags": [{"slug": "weather"}]}, "Will it rain in NYC tomorrow?")
    assert mc3.category == "unclear" and not mc3.insider_plausible


def test_scanner_insider_only_gating(tmp_path):
    trades = [make_trade(ts=NOW - 10, tx="0xsport")]
    # Default config is insider-only: a sports insider produces no alert.
    state, scanner = make_scanner(tmp_path, trades, fresh_profile(), mclass=SPORTS_CLASS)
    scanner.poll_once(NOW)
    assert not state.alerted and not state.is_watched(WALLET)

    # Same fill with --all-markets (insider_only=False) alerts as before.
    cfg = CopyConfig(insider_only=False)
    state2, scanner2 = make_scanner(
        tmp_path / "all", trades, fresh_profile(), cfg=cfg, mclass=SPORTS_CLASS
    )
    scanner2.poll_once(NOW)
    assert len(state2.alerted) == 1


# -- deep backtest / exit simulation ----------------------------------------

from copy_trading.backtest import Candidate as DeepCand  # noqa: E402
from copy_trading.deep_backtest import month_windows, simulate_exit  # noqa: E402


def make_deep_cand(price=0.75, ts=None):
    return DeepCand(
        wallet=WALLET,
        name="w",
        asset="1",
        condition_id="0xc",
        side="BUY",
        outcome="No",
        outcome_index=1,
        title="t",
        slug="s",
        eval_ts=int(ts if ts is not None else NOW - 86400 * 10),
        avg_price=price,
        cash_at_eval=50_000,
    )


def test_simulate_exit_paths():
    t0 = int(NOW - 86400 * 10)
    c = make_deep_cand(0.75, t0)  # entry = 0.76 after 1c slippage
    rising = [
        {"t": t0 + i * 43200, "p": p}
        for i, p in enumerate([0.75, 0.78, 0.82, 0.90, 0.96, 0.97], start=1)
    ]
    end_ts = t0 + 86400 * 30

    r, d, how = simulate_exit(c, rising, 1.0, end_ts, {}, None)
    assert how == "resolution" and r == pytest.approx(1 / 0.76 - 1)

    r, _, how = simulate_exit(c, rising, 1.0, end_ts, {"tp_abs": 0.95}, None)
    assert how == "take-profit" and r == pytest.approx(0.95 / 0.76 - 1)

    falling = [{"t": t0 + 43200, "p": 0.70}, {"t": t0 + 86400, "p": 0.55}]
    r, _, how = simulate_exit(c, falling, 0.0, end_ts, {"sl_minus": 0.15}, None)
    # stop level 0.61; gap-through fills at bar price 0.55 minus 1c
    assert how == "stop" and r == pytest.approx(0.54 / 0.76 - 1)

    late = [{"t": t0 + 86400 * 8, "p": 0.80}]
    r, _, how = simulate_exit(c, late, 1.0, end_ts, {"max_days": 7}, None)
    assert how == "time" and r == pytest.approx(0.79 / 0.76 - 1)

    r, _, how = simulate_exit(c, rising, 1.0, end_ts, {"insider_exit": True}, t0 + 86400)
    assert how == "insider-sold"


def test_month_windows_contiguous():
    w = month_windows(3)
    assert len(w) == 4  # current partial month + 3 full months
    for (s1, e1), (s2, e2) in zip(w, w[1:]):
        assert e1 == s2  # windows tile with no gaps


def test_scanner_skips_near_dollar_parking(tmp_path):
    # $95k buy at 0.98: a cash-parker, not a bet — no alert even when fresh.
    trades = [make_trade(ts=NOW - 10, price=0.98, size=97_000, tx="0xpark")]
    state, scanner = make_scanner(tmp_path, trades, fresh_profile())
    scanner.poll_once(NOW)
    assert not state.alerted
