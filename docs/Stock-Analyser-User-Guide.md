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

- Syncs open positions, **pies**, trades, and dividends from **Trading 212** (or `stocks.xlsx` as fallback).
- Archives **fully sold** holdings to **History → Closed Positions** (removed from the main dashboard).
- Fetches live market data via **yfinance** (resolves legacy T212 tickers, e.g. SOAC → TMC).
- Runs **Claude** (`claude login` in the container) for structured recommendations per holding and per pie.
- Dashboard on port **8765**.
- Writes **Excel/text** reports, **Obsidian** run notes, optional **knowledge notes**, and **MOC** links.

It does **not** place trades.

---

## Report folders at a glance

```text
/data/reports/                          (container — not in Obsidian)
├── full/                               ← Run Analysis (Excel + text)
└── single/                             ← reserved (↻ does not write Excel/text)

10_Personal/13_Finances/Investments/AI Investment Analysis/   (Obsidian)
├── Full Portfolio/                     ← Run Analysis (.md)
│   └── 2026-05-18 Analysis Run 42.md
└── Individual Stock/                   ← ↻ per-card (.md)
    └── 2026-05-18 SOAC Analysis Run 43.md

50_Knowledge/                           ← shared knowledge base (other agents too)
├── notes/                              ← atomic insight notes (sometimes)
└── _moc/
    └── MOC-investment-analysis.md      ← index (auto-created/linked)
```

**Unraid:** you only need the **Obsidian vault path** mapped to `/obsidian`. Env vars are **optional overrides** — the app sets defaults automatically when `/obsidian` exists (newer images).

---

## When is a report created?

| Trigger | Creates |
|---------|---------|
| **Run Analysis** (full portfolio) | Excel + text in `/data/reports/full/`, Obsidian `.md` in **Full Portfolio/**, MOC link |
| **↻** on a card (one ticker/pie) | Obsidian `.md` in **Individual Stock/**, MOC link only |
| **Sync T212** | Nothing |
| **Scheduled** run (Mon/Wed/Sat default) | Same as full **Run Analysis** (after sync if enabled) |

Reports are written **when the run finishes successfully**, not per ticker mid-run.

**Knowledge notes** (`50_Knowledge/notes/`) are written **during** the run, only when Claude returns non-empty `knowledge_notes` (uncommon). Same area as your other agents’ zettelkasten notes.

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
| `…/Investments/AI Investment Analysis/Full Portfolio/` | **Run Analysis** | `YYYY-MM-DD Analysis Run {id}.md` |
| `…/Investments/AI Investment Analysis/Individual Stock/` | **↻** run | `YYYY-MM-DD {TICKER} Analysis Run {id}.md` |
| `50_Knowledge/notes/` | Sometimes | Atomic notes when Claude flags insight |
| `50_Knowledge/_moc/` | Reports & knowledge | `MOC-investment-analysis.md`, topic MOCs |
| `50_Knowledge/compiled/` | Never (this app) | Other agents |
| Rest of vault | Never (this app) | Your other notes |

---

## Dashboard controls

| Control | Claude calls | Disk reports | Obsidian |
|---------|--------------|--------------|----------|
| **Run Analysis** | One per holding + pies | Excel + text → `reports/full/` | `.md` → **Full Portfolio/** |
| **↻** on card | One | None | `.md` → **Individual Stock/** |
| **Sync T212** | None | None | None |

Hint on dashboard: *↻ on a card analyses that position only.*

---

## Dashboard layout: Pies vs Stocks

After **Sync T212**, the dashboard splits holdings into two areas:

| Section | What appears here |
|---------|-------------------|
| **Pies** | Each Trading 212 pie you own (expand for combined analysis + member list) |
| **Stocks** | Positions **not** fully allocated to a pie, or only the slice held **outside** pies |

Stocks that sit **entirely inside a pie** show only under that pie’s **Holdings** rows (click a row for the same detail modal as a full card). They do **not** duplicate as full cards in **Stocks**.

| Action | Scope |
|--------|--------|
| **↻ Analyse** on pie header | Combined pie view (`PIE:{id}`) — one Claude pass for the pie as a portfolio |
| **↻** on a stock card | That ticker only |
| Click a pie holding row | Opens the stock modal for that ticker |

Pie membership comes from T212’s pies API (synced during **Sync T212**; detail calls are rate-limited, so a full pie sync can take several seconds per pie).

---

## Full Run Analysis — what gets analysed

**Run Analysis** (full portfolio) runs Claude on:

1. **Every open position** in your account (each ticker once, at full position size), and  
2. **Every pie** as a synthetic holding (`PIE:{id}`) for a combined pie-level recommendation.

So you get both per-stock write-ups and per-pie portfolio-style analysis. **↻** on a single card or pie runs only that item.

---

## Sold positions and archiving

When you **fully sell** a holding, the app should **remove it from the dashboard** and **archive** it for history.

### Where archived stocks appear

| Location | Contents |
|----------|----------|
| **History → Trades** | **Closed Positions** table at the top (ticker, first bought, sold date, avg cost, realised P&L) |
| **`/ticker/{TICKER}`** | Past analysis and owned-history row if archived |

Archives are stored in SQLite (`owned_history`), not as separate Obsidian files. Obsidian run reports you already generated stay in **Full Portfolio/** or **Individual Stock/**.

### When archiving happens

| Event | Behaviour |
|-------|-----------|
| **Sync T212** | Live T212 positions are authoritative. Tickers T212 no longer reports are **removed** from the active book and **archived** (when trade history exists). |
| **Trade rebuild** | AVCO from imported trades; positions below the dust threshold are treated as flat and archived. |
| **Dashboard load** | Sub-threshold “dust” leftovers (e.g. `0.02` shares from rounding) are purged and archived when possible. |

The dashboard only builds cards for **open** positions (above **0.05 shares**). Old analysis alone does **not** keep a sold ticker on the main grid.

### Dust and rounding

Very small balances (below **0.05 shares**) are treated as **flat** — common after AVCO rounding or fractional leftovers. If T212 still reports a tiny line, sync keeps it until the broker shows zero; sell the remainder on T212 if needed.

### If a sold stock still appears

1. Run **Sync T212** (reconcile + archive).  
2. Refresh the dashboard.  
3. Check **History → Trades → Closed Positions** for the ticker.  
4. If it remains with a non-zero share count, T212 may still list a dust balance — check the T212 app.

---

## Analysis pipeline (per holding)

1. **Resolve market ticker** — e.g. T212 `SOAC` → yfinance `TMC` when wallet price matches.
2. **Fetch yfinance** — price, P/E, news, analyst targets, earnings.
3. **T212 price** — used for position valuation when available.
4. **Handoff memory** — prior thesis; stale “worthless / $0” notes skipped if T212 shows a live price.
5. **Claude** — recommendation, reasoning, catalysts, risks, `handoff_note`, optional `knowledge_notes`.
6. **Knowledge notes** — if any, written to `50_Knowledge/notes/` and linked in `50_Knowledge/_moc/`.
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

### Obsidian — path only (defaults automatic)

1. Add **one** path: host vault → container `/obsidian`
2. **Apply** / restart — no env vars required on newer builds

Optional overrides (only if your layout differs):

| Variable | Built-in default |
|----------|------------------|
| `OBSIDIAN_VAULT_DIR` | `/obsidian` (when mount exists) |
| `OBSIDIAN_REPORTS_SUBDIR` | `10_Personal/13_Finances/Investments/AI Investment Analysis` |
| `OBSIDIAN_REPORTS_FULL_SUBDIR` | `Full Portfolio` |
| `OBSIDIAN_REPORTS_SINGLE_SUBDIR` | `Individual Stock` |
| `OBSIDIAN_KNOWLEDGE_SUBDIR` | `50_Knowledge/notes` |
| `OBSIDIAN_KNOWLEDGE_MOC_DIR` | `50_Knowledge/_moc` |
| `OBSIDIAN_DEFAULT_MOC` | `MOC-investment-analysis` |

Set `OBSIDIAN_KNOWLEDGE_ENABLED=false` to disable knowledge notes only.

### First run

1. Deploy / update container image.
2. `docker exec -it StockAnalyzer claude login`
3. Dashboard → **Sync T212**
4. **Run Analysis** or **↻** on one card

### Verify

```bash
docker exec -it StockAnalyzer ls "/obsidian/10_Personal/13_Finances/Investments/AI Investment Analysis/Full Portfolio"
docker exec -it StockAnalyzer ls "/obsidian/10_Personal/13_Finances/Investments/AI Investment Analysis/Individual Stock"
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
OBSIDIAN_REPORTS_SUBDIR=10_Personal/13_Finances/Investments/AI Investment Analysis
OBSIDIAN_REPORTS_FULL_SUBDIR=Full Portfolio
OBSIDIAN_REPORTS_SINGLE_SUBDIR=Individual Stock
OBSIDIAN_KNOWLEDGE_SUBDIR=50_Knowledge/notes
REPORTS_FULL_SUBDIR=full
REPORTS_SINGLE_SUBDIR=single
OBSIDIAN_KNOWLEDGE_ENABLED=true
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

### No `.md` files in Obsidian

1. **Obsidian vault path** mapped to `/obsidian` (required).
2. **Recent image** with auto-defaults (or set env vars on old images).
3. **Completed** **Run Analysis** or **↻** after restart.
4. Look under:
   - `…/Investments/AI Investment Analysis/Full Portfolio/`
   - `…/Investments/AI Investment Analysis/Individual Stock/`
5. `docker exec StAnalyser env | grep OBSIDIAN` — should show vars after restart on new image.
6. `http://YOUR-IP:8765/api/obsidian/status` → `"ready": true`
7. Logs: `Obsidian report:` not `Obsidian: skipped`

| Symptom | Fix |
|---------|-----|
| No Obsidian `.md` | Vault path + update image + run analysis |
| Empty `grep OBSIDIAN` on old image | Add path only after update, or add env vars manually |
| Wrong folder names | Set `OBSIDIAN_REPORTS_FULL_SUBDIR` / `OBSIDIAN_REPORTS_SINGLE_SUBDIR` or rename folders in Obsidian |
| Old reports in parent folder | Legacy; new runs use **Full Portfolio/** and **Individual Stock/** |
| Stale SOAC analysis | Sync → **↻** |
| Sold stock still on dashboard | **Sync T212** → refresh; see **Sold positions and archiving** |
| Stock only under Pies, not Stocks | Expected when 100% of shares are in that pie |
| Pie empty or missing holdings | Run **Sync T212** (pies sync after trades; allow time per pie) |
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
- `10_Personal/13_Finances/Investments/AI Investment Analysis/Full Portfolio/`
- `10_Personal/13_Finances/Investments/AI Investment Analysis/Individual Stock/`
- `50_Knowledge/notes/` (when created)

---

*Guide version: pies vs Stocks layout, sold-position archiving, per-card ↻, Full Portfolio / Individual Stock report folders, knowledge notes, auto MOC create/link, ticker resolution.*
