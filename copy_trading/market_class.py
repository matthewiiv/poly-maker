"""
Classify Polymarket markets by whether a fresh-wallet insider there could
plausibly be trading on insider knowledge.

A market is insider-plausible when its outcome is decided (or known) by a
small group of humans before the public finds out: legislation getting
signed, an exchange listing a token, a nominee being picked, a company
being acquired, a court ruling. It is NOT insider-plausible when the
outcome is produced in public in real time — sports and esports matches —
or when it is a pure price-level bet, where big fresh wallets are betting
syndicates and traders, not people who know something.

Classification uses Polymarket's own taxonomy (Gamma event tags plus
structural sports fields), with a verb heuristic on the question text as
the tie-breaker. The scanner runs in insider-only mode by default; pass
--all-markets to watch everything.
"""

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from copy_trading.data_api import _get

GAMMA_API = "https://gamma-api.polymarket.com"

# Tag slugs that mark a market as public-competition or mechanical betting.
DENY_TAGS = {
    "sports",
    "games",
    "esports",
    "e-sports",
    "video-games",
    "nfl",
    "nba",
    "mlb",
    "nhl",
    "wnba",
    "ncaa",
    "college-football",
    "college-basketball",
    "soccer",
    "epl",
    "premier-league",
    "la-liga",
    "serie-a",
    "bundesliga",
    "ligue-1",
    "ucl",
    "uel",
    "champions-league",
    "mls",
    "fifa",
    "world-cup",
    "tennis",
    "atp",
    "wta",
    "ufc",
    "mma",
    "boxing",
    "golf",
    "f1",
    "formula-1",
    "nascar",
    "cricket",
    "rugby",
    "olympics",
    "darts",
    "snooker",
    "dota-2",
    "csgo",
    "cs2",
    "counter-strike",
    "league-of-legends",
    "valorant",
    # price-level series: no announcement to be inside on
    "crypto-prices",
    "hit-price",
    "stock-prices",
    "up-or-down",
}

# Tag slugs that positively suggest a small-group-decides outcome.
ALLOW_TAGS = {
    "politics",
    "us-law",
    "legal",
    "crypto-legal",
    "courts",
    "supreme-court",
    "regulation",
    "sec",
    "cftc",
    "fed",
    "white-house",
    "congress",
    "senate",
    "geopolitics",
    "world",
    "israel",
    "ukraine",
    "china",
    "iran",
    "trade-war",
    "business",
    "companies",
    "earnings",
    "ipo",
    "mergers",
    "m-a",
    "listings",
    "coinbase",
    "binance",
    "etf",
    "airdrops",
    "token-launch",
    "trump",
    "cabinet",
    "nominations",
    "appointments",
    "pardons",
}

# Announcement-shaped verbs: the outcome is a discrete human decision or
# disclosure with a knowable-before-public answer.
ANNOUNCE_VERBS = re.compile(
    r"\b(sign(ed|s)?|pass(es|ed)?|approv\w+|list(s|ed|ing)?|launch\w*|announc\w+|"
    r"nominat\w+|confirm\w+|appoint\w+|resign\w*|fir(e|es|ed)|pardon\w*|indict\w+|"
    r"arrest\w*|acquir\w+|merg\w+|ipo|etf|rul(es|ed|ing)|veto\w*|ban(s|ned)?|"
    r"executive order|ceasefire|cease-fire|unveil\w*|releas\w+|step(s)? down|"
    r"drop(s)? out|withdraw\w*|invit\w+|meet(s)?|strike\w* down|repeal\w*)\b",
    re.IGNORECASE,
)

# Price-level bets phrased in the question itself.
PRICE_PATTERN = re.compile(
    r"(\$[\d,.]+[kmb]?|price of|dip to|reach(es)?\s+\$|above|below|between\s+\$|"
    r"close at|all.time high|market cap|up or down)",
    re.IGNORECASE,
)


@dataclass
class MarketClass:
    category: str  # sports | price-market | recurring-series | insider | unclear
    insider_plausible: bool
    reasons: List[str] = field(default_factory=list)


def classify_event(event: Optional[dict], title: str = "") -> MarketClass:
    """
    Pure classification from a Gamma event payload (may be None) and the
    market question text. Deny signals win over allow signals.
    """
    tags = {t.get("slug", "").lower() for t in (event or {}).get("tags", []) if t.get("slug")}
    text = f"{title} {(event or {}).get('title') or ''}"

    # Structural sports markers beat everything: any market in the event
    # carrying a sports market type or a game start time.
    for m in (event or {}).get("markets", []) or []:
        if m.get("sportsMarketType") or m.get("gameStartTime"):
            return MarketClass("sports", False, ["sportsMarketType/gameStartTime"])

    denied = tags & DENY_TAGS
    if denied:
        cat = (
            "price-market"
            if denied & {"crypto-prices", "hit-price", "stock-prices", "up-or-down"}
            else "sports"
        )
        return MarketClass(cat, False, [f"tag:{t}" for t in sorted(denied)])

    if PRICE_PATTERN.search(title or ""):
        return MarketClass("price-market", False, ["price-pattern"])

    # Rapidly recurring series (hourly/daily/weekly) are mechanical markets —
    # sports schedules and price prints — not one-off announcements.
    for s in (event or {}).get("series", []) or []:
        if (s.get("recurrence") or "").lower() in ("hourly", "daily", "weekly"):
            return MarketClass("recurring-series", False, [f"recurrence:{s.get('recurrence')}"])

    allowed = tags & ALLOW_TAGS
    verb = ANNOUNCE_VERBS.search(text)
    if allowed or verb:
        reasons = [f"tag:{t}" for t in sorted(allowed)]
        if verb:
            reasons.append(f"verb:{verb.group(0).lower()}")
        return MarketClass("insider", True, reasons)

    return MarketClass("unclear", False, ["no insider signal"])


class MarketClassifier:
    """Fetches Gamma events by slug (cached) and classifies markets."""

    def __init__(self, fetch_fn: Optional[Callable[[str], Optional[dict]]] = None):
        self.fetch_fn = fetch_fn or self._fetch_event
        self._cache: Dict[str, MarketClass] = {}

    @staticmethod
    def _fetch_event(slug: str) -> Optional[dict]:
        rows = _get(f"{GAMMA_API}/events", {"slug": slug}) or []
        return rows[0] if rows else None

    def classify(self, event_slug: str, title: str = "") -> MarketClass:
        cached = self._cache.get(event_slug)
        if cached is not None:
            return cached
        try:
            event = self.fetch_fn(event_slug) if event_slug else None
        except Exception as ex:
            print(f"[classify] event fetch failed for {event_slug}: {ex}")
            # Fail open as "unclear" but don't cache, so a later fill retries.
            return classify_event(None, title)
        result = classify_event(event, title)
        self._cache[event_slug] = result
        return result
