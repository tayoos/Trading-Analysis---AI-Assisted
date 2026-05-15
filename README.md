# Stock Analyzer — AI-Powered Portfolio Analysis

An AI-powered stock portfolio analyser running as a Docker container.
Reads holdings from Trading 212 (or an Excel file), fetches live market
data, and uses the Claude API to generate Buy/Hold/Sell recommendations
with 30-day price targets, news sentiment, catalysts, risks, and a 90-day outlook.

## Quick start

```bash
cp .env.example .env
# Edit .env — add ANTHROPIC_API_KEY and optionally TRADING212_API_KEY

mkdir -p data/db data/reports data/stocks
# If using Excel fallback: copy your stocks.xlsx into data/stocks/

docker compose up --build
```

Open **http://localhost:8765** and click **Run Analysis Now**.

## Portfolio sources

| Source | Setup | Priority |
|--------|-------|----------|
| Trading 212 API | Set `TRADING212_API_KEY` in `.env` | Primary (auto-syncs) |
| Excel fallback | `data/stocks/stocks.xlsx` with Ticker / Shares / Buy Price columns | Used when no DB positions exist |

## Excel format

| Ticker | Shares | Buy Price |
|--------|--------|-----------|
| AAPL   | 10     | 165.00    |
| NVDA   | 5      | 480.00    |

## Environment variables

See `.env.example` for the full list. Key ones:

| Variable | Default | Notes |
|----------|---------|-------|
| `ANTHROPIC_API_KEY` | — | **Required** |
| `TRADING212_API_KEY` | — | Optional; enables T212 sync |
| `SCHEDULE_DAYS` | `1,3,5` | 0=Mon…6=Sun, comma-separated |
| `SCHEDULE_HOUR` | `7` | UTC hour for scheduled runs |
| `COST_METHOD` | `AVCO` | `AVCO` or `FIFO` |
| `PORT` | `8765` | |

## Unraid deployment

1. Copy `docker-compose.unraid.yml` to your Unraid server, edit `YOURUSERNAME`
2. Set `ANTHROPIC_API_KEY` and `TRADING212_API_KEY` as Unraid template variables
3. `docker compose -f docker-compose.unraid.yml up -d`
4. Watchtower will auto-update the container when new images are pushed to GHCR

## CI/CD

Push to `main` → GitHub Actions builds and pushes `ghcr.io/YOURUSERNAME/stock-analyzer:latest`
→ Watchtower on Unraid pulls and restarts the container automatically.

Edit `.github/workflows/build-push.yml` — replace `YOURUSERNAME` references are inferred
from `github.repository` so no manual edit is needed.

## Architecture

```
Trading 212 API  ──→  sources/t212.py  ──→  portfolio.py (AVCO)
                                                  ↓
                                            SQLite database
                                                  ↓
Excel fallback   ──→  portfolio.py       analyzer.py (Claude API)
                                                  ↓
                                            reports.py (Excel + txt)
                                                  ↓
                                         Flask web UI (port 8765)
```

### Handoff memory system

Each Claude call returns a `handoff_note` JSON block (thesis, watch items,
trend flags, risks, catalysts) stored in SQLite. The next run injects this
note into the prompt (~150 tokens/ticker), giving Claude continuity without
full history context.

### Adding a new data source (e.g. crypto)

1. Create `app/sources/myexchange.py` implementing `DataSource` from `app/sources/base.py`
2. Register it in `create_app()` in `app/__init__.py`
3. Wire it into `portfolio.py` as an additional source

## Project structure

```
app/
├── __init__.py          Flask app factory + APScheduler
├── analyzer.py          Claude analysis engine + handoff notes
├── database.py          SQLite layer
├── portfolio.py         AVCO cost basis + Excel reader
├── reports.py           Excel + text report generation
├── sources/
│   ├── base.py          DataSource ABC (extend for new brokers/exchanges)
│   └── t212.py          Trading 212 REST API client
└── routes/
    ├── dashboard.py     GET /
    ├── analysis.py      POST /api/run, GET /api/status, /api/dashboard
    ├── history.py       GET /history, /api/run/<id>, /ticker/<t>
    └── sync.py          POST /api/sync/t212, GET /api/portfolio
templates/
├── dashboard.html       Main portfolio view
├── history.html         Run history
├── ticker.html          Per-ticker detail + handoff memory
└── sync.html            T212 sync status + trade history
```
