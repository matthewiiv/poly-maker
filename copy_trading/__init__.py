"""
Insider-watching and copy-trading for Polymarket.

This package detects the "fresh wallet suddenly bets six figures" pattern
(the kind of activity covered in articles like "New Polymarket crypto wallet
just bet over $800k on the CLARITY Act") and can optionally mirror those
trades with a small, capped size.

Modules:
- data_api:  thin wrappers over Polymarket's public Data API and CLOB book
- signals:   wallet profiling and insider-signal scoring (pure logic)
- state:     JSON persistence for seen fills, watchlist, and simulated holdings
- executor:  turns an insider signal into a (dry-run or live) copy order
- scanner:   the polling loop that ties everything together
"""

from copy_trading.config import CopyConfig
from copy_trading.signals import WalletProfile, profile_wallet, score_insider
from copy_trading.state import StateStore
from copy_trading.executor import CopyExecutor
from copy_trading.scanner import InsiderScanner

__all__ = [
    "CopyConfig",
    "WalletProfile",
    "profile_wallet",
    "score_insider",
    "StateStore",
    "CopyExecutor",
    "InsiderScanner",
]
