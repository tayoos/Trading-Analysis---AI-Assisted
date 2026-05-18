---
tags:
  - apps
  - homelab
  - stock-analyzer
  - investment
created: 2026-05-18
---

# Stock Analyser — User Guide

How the **Stock Analyser** Docker app works, how to deploy it on **Unraid** or **Proxmox**, and how it connects to your **Obsidian** vault.

Copy this file into your vault (e.g. `Home/Apps/Stock Analyser.md`) and adjust host paths to match your server.

Repository: [Trading-Analysis---AI-Assisted](https://github.com/tayoos/Trading-Analysis---AI-Assisted)

---

## What it does

- Syncs open positions from **Trading 212** (or `stocks.xlsx` as fallback).
- Fetches live market data via **yfinance** (resolves legacy T212 tickers, e.g. SOAC → TMC).
- Runs **Claude** (`claude login` in the container) for structured recommendations per holding.
- Dashboard on port **8765**.
- Writes **Excel/text** reports, **Obsidian** run notes, optional **knowledge notes**, and **MOC** links.

It does **not** place trades.

---

## Report folders at a glance

```text
/data/reports/                          (container — not in Obsidian)
├── full/                               ← Run Analysis (Excel + text)
└── single/                             ← reserved (↻ does not write Excel/text)

10_Personal/13_Finances/AI Investment Analysis/   (Obsidian)
├── Full Portfolio/                     ← Run Analysis (.md)
│   └── 2026-05-18 Analysis Run 42.md
└── Individual Stock/                   ← ↻ per-card (.md)
    └── 2026-05-18 SOAC Analysis Run 43.md

50_Knowledge/
├── notes/                              ← atomic notes (sometimes)
└── _moc/
    └── MOC-investment-analysis.md      ← index (auto-created)
```

---

## When is a report created?

| Trigger | Creates |
|---------|---------|
| **Run Analysis** (full portfolio) | Excel + text in `/data/reports/full/`, Obsidian `.md` in **Full Portfolio/**, MOC link |
| **↻** on a card (one ticker/pie) | Obsidian `.md` in **Individual Stock/**, MOC link only |
| **Sync T212** | Nothing |
| **Scheduled** run (Mon/Wed/Sat default) | Same as full **Run Analysis** (after sync if enabled) |

Reports are written **when the run finishes successfully**, not per ticker mid-run.

**Knowledge notes** (`50_Knowledge/notes/`) are written **during** the run, only when Claude returns non-empty `knowledge_notes` (uncommon).

Only **one** analysis (full or single) can run at a time.

---

## High-level architecture

```mermaid
flowchart LR
  subgraph host [Unraid / Proxmox]
    T212[Trading 212 API]
    Vault[Obsidian vault]
    AppData[stock-analyzer appdata]
  end

  subgraph container [Stock Analyser]
    Web[Dashboard :8765]
    DB[(SQLite)]
    Claude[Claude Agent SDK]
    Reports[/data/reports]
    ObsMount[/obsidian]
  end

  T212 --> AppData
  AppData --> DB
  Vault --> ObsMount
  Web --> Claude
  Claude --> DB
  Claude --> Reports
  Claude --> ObsMount
```

---

## Docker paths

| Container path | Host (example Unraid) | Purpose |
|----------------|------------------------|---------|
| `/data` | `/mnt/user/appdata/stock-analyzer` | DB, `/data/reports`, logs |
| `/obsidian` | `/mnt/user/appdata/obsidian/MyVault` | **Vault root** (contains `10_Personal`, `50_Knowledge`, …) |
| `/home/appuser/.claude` | `.../stock-analyzer/claude-auth` | Claude session |

Mount the **vault root**, not `AI Investment Analysis` or `50_Knowledge` alone.

---

## Vault output map

| Path | When | Contents |
|------|------|----------|
| `…/AI Investment Analysis/Full Portfolio/` | Every successful **Run Analysis** | `YYYY-MM-DD Analysis Run {id}.md` |
| `…/AI Investment Analysis/Individual Stock/` | Every successful **↻** run | `YYYY-MM-DD {TICKER} Analysis Run {id}.md` |
| `50_Knowledge/notes/` | Sometimes | `YYYYMMDDHHMMSS-{slug}.md` |
| `50_Knowledge/_moc/` | Create/link on reports & knowledge | `MOC-investment-analysis.md`, topic MOCs |
| `50_Knowledge/compiled/` | Never (this app) | Other agents |
| Rest of vault | Never (this app) | Other Claude agents, your notes |

---

## Dashboard controls

| Control | Claude calls | Disk reports | Obsidian |
|---------|--------------|--------------|----------|
| **Run Analysis** | One per holding + pies | Excel + text → `reports/full/` | `.md` → **Full Portfolio/** |
| **↻** on card | One | None | `.md` → **Individual Stock/** |
| **Sync T212** | None | None | None |

Hint on dashboard: *↻ on a card analyses that position only.*

---

## Analysis pipeline (per holding)

1. **Resolve market ticker** — e.g. T212 `SOAC` → yfinance `TMC` when wallet price matches.
2. **Fetch yfinance** — price, P/E, news, analyst targets, earnings.
3. **T212 price** — used for position valuation when available.
4. **Handoff memory** — prior thesis; stale “worthless / $0” notes skipped if T212 shows a live price.
5. **Claude** — recommendation, reasoning, catalysts, risks, `handoff_note`, optional `knowledge_notes`.
6. **Knowledge notes** — if any, written to `50_Knowledge/notes/` and linked in MOCs.
7. **SQLite** — analysis row + handoff for next run.

---

## MOC-investment-analysis

Auto-created at `50_Knowledge/_moc/MOC-investment-analysis.md` if missing.

| Section | Linked from |
|---------|-------------|
| **## Full Portfolio runs** | Full **Run Analysis** `.md` files |
| **## Individual Stock runs** | **↻** run `.md` files |
| **## Knowledge notes** | Atomic notes in `50_Knowledge/notes/` |

Topic MOCs (e.g. `MOC-uk-reits`) are created/linked only when Claude lists them on a **knowledge note**, not on run reports.

Each run report `.md` includes frontmatter `moc: '[[MOC-investment-analysis]]'`.

---

## Knowledge notes (selective)

Claude returns `knowledge_notes: []` on most runs.

**Written when** insight is durable and reusable, e.g.:

- T212 code vs market symbol mapping
- Sector/framework insight beyond one trade
- Material thesis change or correcting a misconception

**Not written for** routine recs, targets-only, or P&L summaries.

**MOCs:** always `MOC-investment-analysis`; plus any extra `mocs` Claude suggests (e.g. `MOC-uk-reits`). Same `slug` updates the existing note file.

---

## T212 legacy tickers (SOAC / TMC)

| UI | Meaning |
|----|---------|
| Company name as title | From T212 / yfinance |
| `SOAC · quotes TMC` | DB key `SOAC`; quotes/analysis data use `TMC` |

Stale “SOAC worthless $0” on a card → **Sync T212**, then **↻** on that card after deploying the latest image.

---

## Unraid setup

### Paths (Add Container)

| Name | Container | Host |
|------|-----------|------|
| Data directory | `/data` | `/mnt/user/appdata/stock-analyzer` |
| Claude credentials | `/home/appuser/.claude` | `.../stock-analyzer/claude-auth` |
| Obsidian vault | `/obsidian` | `/mnt/user/appdata/obsidian/MyVault` |

### Environment (minimum)

`TRADING212_API_KEY`, `TRADING212_API_SECRET`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD`, `TZ`

### Obsidian environment

| Variable | Default |
|----------|---------|
| `OBSIDIAN_VAULT_DIR` | `/obsidian` |
| `OBSIDIAN_REPORTS_SUBDIR` | `10_Personal/13_Finances/AI Investment Analysis` |
| `OBSIDIAN_REPORTS_FULL_SUBDIR` | `Full Portfolio` |
| `OBSIDIAN_REPORTS_SINGLE_SUBDIR` | `Individual Stock` |
| `REPORTS_FULL_SUBDIR` | `full` |
| `REPORTS_SINGLE_SUBDIR` | `single` |
| `OBSIDIAN_KNOWLEDGE_ENABLED` | `true` |
| `OBSIDIAN_KNOWLEDGE_SUBDIR` | `50_Knowledge/notes` |
| `OBSIDIAN_KNOWLEDGE_MOC_DIR` | `50_Knowledge/_moc` |
| `OBSIDIAN_DEFAULT_MOC` | `MOC-investment-analysis` |

Set `OBSIDIAN_KNOWLEDGE_ENABLED=false` to disable `50_Knowledge/notes` only (run reports still work).

### First run

1. Deploy / update container image.
2. `docker exec -it StockAnalyzer claude login`
3. Dashboard → **Sync T212**
4. **Run Analysis** or **↻** on one card

### Verify

```bash
docker exec -it StockAnalyzer ls "/obsidian/10_Personal/13_Finances/AI Investment Analysis/Full Portfolio"
docker exec -it StockAnalyzer ls "/obsidian/10_Personal/13_Finances/AI Investment Analysis/Individual Stock"
docker exec -it StockAnalyzer ls "/obsidian/50_Knowledge/_moc"
```

---

## Proxmox setup

Use `docker-compose.proxmox.yml`. Example `.env`:

```bash
PRIMARY_DATA_PATH=/opt/stock-analyzer
BACKUP_HOST_PATH=/mnt/backups/stock-analyzer
OBSIDIAN_VAULT_HOST_PATH=/mnt/obsidian/MyVault
OBSIDIAN_VAULT_DIR=/obsidian
OBSIDIAN_REPORTS_SUBDIR=10_Personal/13_Finances/AI Investment Analysis
OBSIDIAN_REPORTS_FULL_SUBDIR=Full Portfolio
OBSIDIAN_REPORTS_SINGLE_SUBDIR=Individual Stock
REPORTS_FULL_SUBDIR=full
REPORTS_SINGLE_SUBDIR=single
OBSIDIAN_KNOWLEDGE_ENABLED=true
OBSIDIAN_KNOWLEDGE_SUBDIR=50_Knowledge/notes
OBSIDIAN_KNOWLEDGE_MOC_DIR=50_Knowledge/_moc
OBSIDIAN_DEFAULT_MOC=MOC-investment-analysis
```

---

## Handoff memory

Stored in SQLite (`handoff_notes`), not as separate Obsidian files. Feeds the next run for that ticker unless stale.

---

## Schedule

| Variable | Default |
|----------|---------|
| `SCHEDULE_ENABLED` | `true` |
| `SCHEDULE_DAYS` | `0,2,5` (Mon, Wed, Sat) |
| `SCHEDULE_HOUR` | `3` |
| `T212_SYNC_ENABLED` | `true` |

---

## Security

`DASHBOARD_USER` / `DASHBOARD_PASSWORD`, `TRUSTED_NETWORKS` (proxy bypass), optional `REPORTS_ENCRYPTION_KEY` for Excel/text.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No Obsidian `.md` | Update image; check `/obsidian` mount + `OBSIDIAN_VAULT_DIR` |
| Wrong folder names | Set `OBSIDIAN_REPORTS_FULL_SUBDIR` / `OBSIDIAN_REPORTS_SINGLE_SUBDIR` or rename folders in Obsidian |
| Old reports in parent folder | Legacy; new runs use **Full Portfolio/** and **Individual Stock/** |
| Stale SOAC analysis | Sync → **↻** |
| No knowledge notes | Normal (most runs use `[]`) |
| No topic MOC link | Claude must list `mocs` on a knowledge note |
| Analysis already running | Wait for current run |
| Claude auth / quota | `claude login`; check subscription limits |
| Watchtower missing Obsidian path | Edit container once (Watchtower only swaps image) |

---

## Full environment reference

### Core

`DB_PATH`, `REPORTS_DIR`, `EXCEL_PATH`, `LOG_DIR`, `PORT`, `COST_METHOD`, `REPORTS_RETENTION_DAYS`, `REPORTS_ENCRYPTION_KEY`

### Obsidian (see table above)

### Compose host only

`ZFS_APPDATA_PATH`, `OBSIDIAN_VAULT_HOST_PATH`, `PRIMARY_DATA_PATH`, `BACKUP_HOST_PATH`

---

## Place in your vault

```text
Home/
  Apps/
    Stock Analyser.md
```

Link from `[[MOC-homelab]]` or `[[MOC-investment-analysis]]`.

### Generated links to expect

- `[[MOC-investment-analysis]]`
- `10_Personal/13_Finances/AI Investment Analysis/Full Portfolio/`
- `10_Personal/13_Finances/AI Investment Analysis/Individual Stock/`
- `50_Knowledge/notes/` (when created)

---

*Guide version: per-card ↻, Full Portfolio / Individual Stock report folders, knowledge notes, auto MOC create/link, ticker resolution.*
