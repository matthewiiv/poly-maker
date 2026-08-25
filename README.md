> [!WARNING]
> In today's market, this bot is not profitable and will lose money. Use it as a reference implementation for building your own market making strategies, not as a ready-to-deploy solution. Given the increased competition on Polymarket, I don't see a point in playing with this unless you're willing to dedicate a significant amount of time.
 
# Poly-Maker

A market making bot for Polymarket prediction markets. This bot automates the process of providing liquidity to markets on Polymarket by maintaining orders on both sides of the book with configurable parameters. A summary of my experience running this bot is available [here](https://x.com/defiance_cr/status/1906774862254800934)

## Overview

Poly-Maker is a comprehensive solution for automated market making on Polymarket. It includes:

- Real-time order book monitoring via WebSockets
- Position management with risk controls
- Customizable trade parameters fetched from Google Sheets
- Automated position merging functionality
- Sophisticated spread and price management

## Structure

The repository consists of several interconnected modules:

- `poly_data`: Core data management and market making logic
- `poly_merger`: Utility for merging positions (based on open-source Polymarket code)
- `poly_stats`: Account statistics tracking
- `poly_utils`: Shared utility functions
- `data_updater`: Separate module for collecting market information
- `copy_trading`: Standalone whale watcher / copy trader (see below)

## Whale Watching & Copy Trading

Ever read a headline like *"new Polymarket wallet just bet $800k on the CLARITY Act"* and wondered whether you could spot those wallets live — and ride along? `watch_whales.py` does exactly that, using only Polymarket's public APIs:

1. **Tape scan** — polls `data-api.polymarket.com/trades` with a cash filter, so every fill on the platform worth ≥ $25k (configurable) is examined seconds after it prints.
2. **Wallet forensics** — pulls the wallet's full `/activity` history: how old is it, how many lifetime trades, how concentrated? A wallet whose *first-ever action* is a six-figure conviction buy is the classic "insider-looking" pattern.
3. **Scoring** — transparent 0–100 score (freshness, bet size, concentration, conviction pricing). Above the threshold: alert (console + optional Slack/Discord webhook), auto-watchlist, and optionally a copy.
4. **Copying** — mirrors the whale with a tiny proportional size (default 0.1% of their bet, capped per trade/market/total), as a marketable limit order that refuses to chase if the price already moved more than a few cents past the whale's fill. Watchlisted wallets' follow-up buys and exits are mirrored too.

**Insider-only by default.** Most fresh-wallet whales are sports syndicates, not insiders, so the scanner only alerts on *insider-plausible* markets — outcomes a small group of humans knows before the public (legislation, listings, appointments, rulings, M&A). Classification uses Polymarket's own event tags plus structural signals (`sportsMarketType`/`gameStartTime`, price-series tags, rapid recurring series) with an announcement-verb fallback on the question text — see `copy_trading/market_class.py`. Pass `--all-markets` to watch everything, sports included.

```bash
# Alert-only (no orders, no credentials needed)
uv run python watch_whales.py

# Dry-run copying: prints the exact orders it WOULD place
uv run python watch_whales.py --copy

# Follow specific wallets you already know about
uv run python watch_whales.py --copy --wallets 0xabc...,0xdef...

# Live copying (real money!) — needs PK/BROWSER_ADDRESS plus an explicit opt-in
COPY_TRADER_LIVE=YES uv run python watch_whales.py --copy --live

# One diagnostic pass over the last 24h of big prints
uv run python watch_whales.py --once --lookback 86400 --copy
```

Run `uv run python watch_whales.py --help` for all knobs (thresholds, copy ratio, caps, slippage, poll rate, webhook).

### Backtesting

`copy_trading/backtest.py` replays the reachable trade tape (~50 days at a $25k filter) through the exact scanner logic — wallet profiles are reconstructed *as of each fill's timestamp* (no lookahead) — and grades hypothetical copies against market resolutions:

```bash
uv run python -m copy_trading.backtest --sweep            # full sweep + sensitivity matrix
uv run python -m copy_trading.backtest --case-study <conditionId>   # replay one market's whales
```

Findings from the Jul 7 – Aug 25 2026 window (10,100 fills ≥ $25k, 6,485 whale-buy buckets, 6,208 resolved):

| group | n resolved | win rate | avg return / $1 (1¢ slippage) |
|---|---|---|---|
| alerts score ≥ 75 | 126 | 65.9% | **+5.2%** |
| all alerts (score ≥ 60) | 197 | 61.9% | −1.3% |
| control: all other big buys | 6,011 | 68.5% | −1.9% |

The fresh-wallet filter finds genuine signal at the higher score bands (edge survives up to ~3¢ of slippage), but the median alert is a **sports syndicate** bankrolling a disposable wallet, not a political insider — and fresh whales lose too (one dropped $517k on "France to advance" and got zeroed). Treat the score as a filter, not an oracle.

**Know what you're buying.** Copy trading whales is *not* free money, and this tool defaults to dry-run for a reason:

- **You're late by design.** By the time a whale's prints hit the tape the book has often already repriced — the slippage guard will skip many of the juiciest signals (correctly).
- **"Insider-looking" ≠ informed.** Fresh wallets bet big on *both* sides of the same market (the CLARITY Act market had fresh six-figure whales on YES *and* NO simultaneously). Some are hedging exposure elsewhere, some are laundering attention, some are just rich and wrong.
- **Adverse selection cuts both ways.** If the whale really is informed, the people selling to you are the ones who know less — but if they're not, you've bought a moved price on noise.
- **Sells only close copies.** The tool never shorts; a whale SELL just exits whatever you copied earlier.

## Requirements

- Python 3.9.10 or higher
- Node.js (for poly_merger)
- Google Sheets API credentials
- Polymarket account and API credentials

## Installation

This project uses UV for fast, reliable package management.

### Install UV

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Or with pip
pip install uv
```

### Install Dependencies

```bash
# Install all dependencies
uv sync

# Install with development dependencies (black, pytest)
uv sync --extra dev
```

### Quick Start

```bash
# Run the market maker (recommended)
uv run python main.py

# Update market data
uv run python update_markets.py

# Update statistics
uv run python update_stats.py
```

### Setup Steps

#### 1. Clone the repository

```bash
git clone https://github.com/yourusername/poly-maker.git
cd poly-maker
```

#### 2. Install Python dependencies

```bash
uv sync
```

#### 3. Install Node.js dependencies for the merger

```bash
cd poly_merger
npm install
cd ..
```

#### 4. Set up environment variables

```bash
cp .env.example .env
```

#### 5. Configure your credentials in `.env`

Edit the `.env` file with your credentials:
- `PK`: Your private key for Polymarket
- `BROWSER_ADDRESS`: Your wallet address

**Important:** Make sure your wallet has done at least one trade through the UI so that the permissions are proper.

#### 6. Set up Google Sheets integration

- Create a Google Service Account and download credentials to the main directory
- Copy the [sample Google Sheet](https://docs.google.com/spreadsheets/d/1Kt6yGY7CZpB75cLJJAdWo7LSp9Oz7pjqfuVWwgtn7Ns/edit?gid=1884499063#gid=1884499063)
- Add your Google service account to the sheet with edit permissions
- Update `SPREADSHEET_URL` in your `.env` file

#### 7. Update market data

Run the market data updater to fetch all available markets:

```bash
uv run python update_markets.py
```

This should run continuously in the background (preferably on a different IP than your trading bot).

- Add markets you want to trade to the "Selected Markets" sheet
- Select markets from the "Volatility Markets" sheet
- Configure parameters in the "Hyperparameters" sheet (default parameters that worked well in November are included)

#### 8. Start the market making bot

```bash
uv run python main.py
```

## Configuration

The bot is configured via a Google Spreadsheet with several worksheets:

- **Selected Markets**: Markets you want to trade
- **All Markets**: Database of all markets on Polymarket
- **Hyperparameters**: Configuration parameters for the trading logic


## Poly Merger

The `poly_merger` module is a particularly powerful utility that handles position merging on Polymarket. It's built on open-source Polymarket code and provides a smooth way to consolidate positions, reducing gas fees and improving capital efficiency.

## Important Notes

- This code interacts with real markets and can potentially lose real money
- Test thoroughly with small amounts before deploying with significant capital
- The `data_updater` is technically a separate repository but is included here for convenience

## License

MIT
