"""
On-chain wallet intelligence for the insider scanner.

Three capabilities, all validated against the October 2025 Nobel Peace Prize
front-running episode (see the development log, entries 10-12):

1. FundingTracer — who funded a wallet, on-chain (Polygon via Blockscout).
   Token transfers are whitelisted by CONTRACT ADDRESS (address-poisoning
   scams imitate token symbols with Cyrillic lookalikes), and funders that
   are contracts (Polymarket's own CTFExchange, ConditionalTokens, bridges)
   are never treated as parents.

2. CoordinationBook — the one structure that recovered a documented insider
   ring blind: two or more freshly-funded wallets sharing one EOA funder,
   betting the same market-outcome within days. In the Nobel episode one
   burner parent funded ten fresh wallets over a few hours; eight of them
   bought the winning outcome before the public announcement. A live
   coordination alert would have fired when the second sibling bet, hours
   ahead of the news.

3. deposit_fingerprint — "deposit sized to the bet, minutes before betting"
   (the CLARITY-cluster signature). Descriptive only: measured across 19
   months this fingerprint alone is neutral-to-NEGATIVE for returns; it
   marks disposable-wallet behavior, not profitable information.

Everything here is alert enrichment: fail-open, rate-limited, and injectable
for tests. Nothing blocks the scanner if the explorer is down.
"""

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

BLOCKSCOUT_API = "https://polygon.blockscout.com/api"

# Real token contracts on Polygon (lowercase). Symbols are NOT trusted:
# scam tokens imitate "USDC" with homoglyphs to poison transfer histories.
TOKEN_CONTRACTS = {
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "PUSD",
}

ZERO = "0x" + "0" * 40


@dataclass
class Funding:
    funder: str  # lowercase address; ZERO for bridge mints
    ts: int
    usd: float
    token: str
    mint: bool  # bridge/deposit mint (funder identity not on this chain)
    funder_is_contract: Optional[bool] = None


class FundingTracer:
    """Blockscout-backed funding lookups with in-memory caches."""

    def __init__(self, session: Optional[requests.Session] = None, min_usd: float = 100.0):
        self.http = session or requests.Session()
        self.min_usd = min_usd
        self._funding: Dict[str, Optional[Funding]] = {}
        self._contract: Dict[str, bool] = {}
        self._last_call = 0.0

    def _throttle(self):
        wait = 0.34 - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _get(self, params: dict):
        self._throttle()
        r = self.http.get(BLOCKSCOUT_API, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def is_contract(self, addr: str) -> bool:
        addr = addr.lower()
        if addr not in self._contract:
            self._throttle()
            r = self.http.get(f"{BLOCKSCOUT_API}/v2/addresses/{addr}", timeout=20)
            r.raise_for_status()
            self._contract[addr] = bool(r.json().get("is_contract"))
        return self._contract[addr]

    def first_funding(self, wallet: str) -> Optional[Funding]:
        """Earliest whitelisted incoming transfer >= min_usd, or None."""
        wallet = wallet.lower()
        if wallet in self._funding:
            return self._funding[wallet]
        try:
            rows = (
                self._get(
                    {
                        "module": "account",
                        "action": "tokentx",
                        "address": wallet,
                        "sort": "asc",
                        "page": 1,
                        "offset": 40,
                    }
                ).get("result")
                or []
            )
            if isinstance(rows, str):
                rows = []
        except Exception:
            return None  # fail open, do not cache
        found = None
        for t in rows:
            if (t.get("contractAddress") or "").lower() not in TOKEN_CONTRACTS:
                continue
            if (t.get("to") or "").lower() != wallet:
                continue
            usd = int(t.get("value") or 0) / 10 ** int(t.get("tokenDecimal") or 6)
            if usd < self.min_usd:
                continue
            frm = (t.get("from") or "").lower()
            found = Funding(
                funder=frm,
                ts=int(t.get("timeStamp") or 0),
                usd=round(usd, 2),
                token=TOKEN_CONTRACTS[(t.get("contractAddress") or "").lower()],
                mint=frm == ZERO,
            )
            if not found.mint:
                try:
                    found.funder_is_contract = self.is_contract(found.funder)
                except Exception:
                    found.funder_is_contract = None
            break
        self._funding[wallet] = found
        return found


def deposit_fingerprint(funding: Optional[Funding], bet_ts: int, bet_cash: float) -> List[str]:
    """Descriptive tags for how the bet was bankrolled (not a return signal)."""
    if funding is None:
        return []
    tags = []
    age_h = (bet_ts - funding.ts) / 3600.0
    if 0 <= age_h <= 24:
        tags.append(f"deposit-jit({age_h:.1f}h)")
    if funding.usd > 0 and 0.5 <= bet_cash / funding.usd <= 1.1:
        tags.append("deposit-sized-to-bet")
    if funding.mint:
        tags.append("deposit-bridge-mint")
    elif funding.funder_is_contract is False:
        tags.append(f"funder({funding.funder[:10]}…)")
    return tags


@dataclass
class CoordinationHit:
    funder: str
    wallets: List[str]
    market_key: str  # token id: one market-outcome
    total_cash: float


class CoordinationBook:
    """Tracks fresh-wallet alerts by funder; fires when siblings converge.

    Records (funder -> alerts) and reports a CoordinationHit when at least
    `min_wallets` distinct wallets funded by the same non-contract EOA have
    alerted on the same (condition, outcome) within `window_secs`.
    """

    def __init__(self, min_wallets: int = 2, window_secs: int = 7 * 86400):
        self.min_wallets = min_wallets
        self.window_secs = window_secs
        self._by_funder: Dict[str, List[dict]] = {}

    def to_dict(self) -> dict:
        return {"by_funder": self._by_funder}

    @classmethod
    def from_dict(cls, d: dict, **kw) -> "CoordinationBook":
        book = cls(**kw)
        book._by_funder = {k: list(v) for k, v in (d.get("by_funder") or {}).items()}
        return book

    def record(
        self,
        funding: Optional[Funding],
        wallet: str,
        market_key: str,
        ts: int,
        cash: float,
    ) -> Optional[CoordinationHit]:
        """Record an alerted fresh-wallet bet; return a hit if siblings align.

        market_key should identify one market-outcome (the CLOB token id).
        """
        if funding is None or funding.mint or funding.funder_is_contract:
            return None
        rec = {
            "wallet": wallet.lower(),
            "mk": market_key,
            "ts": int(ts),
            "cash": float(cash),
        }
        rows = self._by_funder.setdefault(funding.funder, [])
        if not any(r["wallet"] == rec["wallet"] and r["mk"] == rec["mk"] for r in rows):
            rows.append(rec)
        sibs = [r for r in rows if r["mk"] == market_key and abs(ts - r["ts"]) <= self.window_secs]
        wallets = sorted({r["wallet"] for r in sibs})
        if len(wallets) >= self.min_wallets:
            return CoordinationHit(
                funder=funding.funder,
                wallets=wallets,
                market_key=market_key,
                total_cash=sum(r["cash"] for r in sibs),
            )
        return None
