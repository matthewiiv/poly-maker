# CLAUDE.md

## Presenting work

Present substantive work on the copy-trading system (analyses, backtests,
detections, model changes) in the Claude artifact **"The Insider Tape"** —
https://claude.ai/code/artifact/aa05c872-0e52-401a-83ea-0da4daf33afb — which
is an append-only development log:

- Add a new numbered, dated entry per milestone at the TOP of the log
  (newest first), with a link first in the masthead index; republish to the
  same URL.
- Never rewrite or remove earlier entries beyond fixing typos — later
  findings supersede earlier ones in a new entry, not by editing history.
- No whale terminology: the project's language is insider-centric
  (watch_insiders.py, InsiderScanner, INSIDER ALERT).

## Adversarial review

Before presenting any new "finding" (a profitable cut, filter, or strategy
tweak) as real, spin up parallel adversarial agents to attack it — at
minimum: a statistics skeptic (overfitting, multiple comparisons,
effective sample size after clustering), a mechanism skeptic (does the
feature measure what we claim, or is it a proxy/patch — e.g. test wallet
features price-stratified), a data-integrity auditor (lookahead,
survivorship, selection), and an economic-realism critic (null models
like favorite-longshot bias, capacity, adverse selection). Report which
claims survived, weakened, or died — killed findings go in the log too.

## Project notes

- `copy_trading/` is the insider watcher / copy trader; `watch_insiders.py`
  is its CLI. It is insider-only by default (see `copy_trading/market_class.py`);
  `--all-markets` restores the broad feed.
- Backtests: `uv run python -m copy_trading.backtest` (point-in-time wallet
  profiles — no lookahead; results cached under `copy_trading/backtest_cache/`).
- Tests: `uv run pytest tests/` (network-free; API access is injected).
  Format new code with `uv run black` (repo config, line length 100).
