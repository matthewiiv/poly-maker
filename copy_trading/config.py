from dataclasses import dataclass
from typing import Optional


@dataclass
class CopyConfig:
    """
    Tunable parameters for insider detection and copy trading.

    All cash amounts are in USDC. Prices are probabilities in (0, 1).
    """

    # --- Detection ---------------------------------------------------------
    # Minimum cash value (size * price) of a single fill on the platform-wide
    # trade tape for us to even look at it.
    min_trade_cash: float = 25_000.0

    # A wallet/asset/side "bucket" (fills grouped within aggregation_window_secs)
    # whose score reaches this threshold triggers an alert (and a copy, if
    # copying is enabled). Score is 0-100, see signals.score_insider.
    alert_score: int = 60

    # Wallet age bands used by the scorer. A wallet whose first on-platform
    # activity is under fresh_wallet_days old is the classic "new wallet
    # appears and bets big" pattern.
    fresh_wallet_days: float = 3.0
    young_wallet_days: float = 14.0

    # A wallet with at most this many lifetime trades counts as inexperienced.
    few_trades_threshold: int = 5

    # Cash size that counts as a headline-grade bet on its own.
    big_bet_usdc: float = 100_000.0

    # Insiders rarely fill in one print; fills from the same wallet on the same
    # token and side within this window are aggregated into one signal.
    aggregation_window_secs: float = 180.0

    # Only alert on insider-plausible markets (legislation, listings,
    # appointments, rulings — outcomes a small group knows before the
    # public). Sports/esports, price-level bets and rapid recurring series
    # are excluded: big fresh wallets there are betting syndicates, not insiders.
    # Disable via --all-markets to watch everything.
    insider_only: bool = True

    # Ignore buys above this price: fresh wallets buying at 95-99.9¢ are
    # parking cash on a near-certainty for a fraction of a percent (the
    # year-scale backtest found $44M of them) — nothing copyable there.
    max_alert_price: float = 0.95

    # Optionally ignore buys below this price too. NOTE (red-team audit, log
    # entry 08-09): price cuts like this are favorite-longshot-bias filters
    # that work for ANY large buyer — they are not evidence of insider
    # identification, and the 65¢ value sat at the 58th percentile of a
    # pure-noise selection null. Off by default.
    min_alert_price: float = 0.0

    # --- Copying -----------------------------------------------------------
    # Fraction of the insider's cash size that we mirror (0.001 = $100 copy of a
    # $100k bet), further limited by the caps below.
    copy_ratio: float = 0.001

    # Hard caps on our own exposure.
    max_per_trade_usdc: float = 100.0
    max_per_market_usdc: float = 250.0
    max_total_usdc: float = 1_000.0

    # Skip the copy if the market has already moved more than this many
    # probability points past the insider's average fill price (chasing guard).
    max_slippage: float = 0.03

    # Don't bother placing dust orders below this cash value.
    min_copy_cash: float = 1.0

    # Once a wallet is on the watchlist, mirror its later trades when they are
    # at least this large (much lower than min_trade_cash so we catch its
    # follow-ups and exits).
    follow_min_cash: float = 500.0

    # --- Ops ---------------------------------------------------------------
    poll_secs: float = 4.0
    state_path: str = "copy_trading/state.json"

    # Optional webhook (Slack/Discord compatible) for insider alerts.
    webhook_url: Optional[str] = None
