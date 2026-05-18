---
tags:
  - apps
  - homelab
  - stock-analyzer
  - investment
created: 2026-05-18
---

# Stock Analyser — User Guide

This document describes how the **Stock Analyser** Docker app works, how to deploy it on **Unraid** or **Proxmox**, and how it connects to your **Obsidian** vault. Copy this file into your vault (for example `Home/Apps/Stock Analyser.md`) and keep it updated when you change container settings.

The app repository is: [Trading-Analysis---AI-Assisted](https://github.com/tayoos/Trading-Analysis---AI-Assisted)

---

## What it does

- Syncs open positions from **Trading 212** (or reads `stocks.xlsx` as a fallback).
- Fetches live market data via **yfinance** (with ticker resolution when T212 uses legacy codes).
- Runs **Claude** (via your subscription — `claude login` in the container) to produce structured recommendations per holding.
- Shows results on a web dashboard (port **8765**).
- Writes optional **reports** (Excel, text) and **Obsidian** notes into your vault.

It does **not** place trades. It is analysis and record-keeping only.

---

## High-level architecture

```mermaid
flowchart LR
  subgraph unraid [Unraid / Proxmox host]
    T212[Trading 212 API]
    Vault[Obsidian vault on disk]
    AppData[/data appdata]
  end

  subgraph container [Stock Analyser container]
    Web[Dashboard :8765]
    DB[(SQLite)]
    Claude[Claude Agent SDK]
    Reports[/data/reports]
    ObsidianMount[/obsidian]
  end

  T212 --> AppData
  AppData --> DB
  Vault --> ObsidianMount
  Web --> Claude
  Claude --> DB
  Claude --> Reports
  Claude --> ObsidianMount
```

---

## Storage map (one vault mount, two purposes)

When Obsidian is enabled you mount the **vault root** once. The app writes to different folders inside it.

| Container path | Host (example Unraid) | Purpose |
|----------------|------------------------|---------|
| `/data` | `/mnt/user/appdata/stock-analyzer` | SQLite DB, Excel/text reports, logs, backups |
| `/obsidian` | `/mnt/user/appdata/obsidian/MyVault` | Your full Obsidian vault (read/write) |
| `/home/appuser/.claude` | `.../stock-analyzer/claude-auth` | Claude login session |

Inside the vault the app uses:

| Vault path | Written by app? | Contents |
|------------|-----------------|----------|
| `10_Personal/13_Finances/AI Investment Analysis/` | Yes — every successful run | Dated run reports: `2026-05-18 Analysis Run 42.md` |
| `50_Knowledge/notes/` | Sometimes | Atomic notes when Claude flags durable insight |
| `50_Knowledge/_moc/` | Yes — create/link | `MOC-investment-analysis.md` + topic MOCs when suggested |
| `50_Knowledge/compiled/` | No | Your/other agents’ compilations |
| Rest of vault | No | Other agents (e.g. homelab, SysML) — untouched |

You do **not** need a separate Docker path for `50_Knowledge` or `AI Investment Analysis` — only the vault root.

---

## Unraid setup

### Required container paths

| Name | Container path | Host path (adjust yours) |
|------|----------------|---------------------------|
| Data directory | `/data` | `/mnt/user/appdata/stock-analyzer` |
| Claude credentials | `/home/appuser/.claude` | `/mnt/user/appdata/stock-analyzer/claude-auth` |
| Obsidian vault (optional) | `/obsidian` | `/mnt/user/appdata/obsidian/MyVault` |

`MyVault` must be the folder that **contains** `10_Personal/`, `50_Knowledge/`, etc. — not a subfolder inside them.

Backups (optional): map a host folder to `/backups` or use `/data/backups` on the array.

### Required environment variables

| Variable | Example | Purpose |
|----------|---------|---------|
| `TRADING212_API_KEY` | (secret) | T212 API |
| `TRADING212_API_SECRET` | (secret) | T212 API |
| `DASHBOARD_USER` | `admin` | Basic auth |
| `DASHBOARD_PASSWORD` | (secret) | Basic auth |
| `TZ` | `Europe/London` | Schedule + display timezone |

### Obsidian environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OBSIDIAN_VAULT_DIR` | `/obsidian` | Must match container mount |
| `OBSIDIAN_REPORTS_SUBDIR` | `10_Personal/13_Finances/AI Investment Analysis` | Run report folder |
| `OBSIDIAN_KNOWLEDGE_ENABLED` | `true` | Allow atomic knowledge notes |
| `OBSIDIAN_KNOWLEDGE_SUBDIR` | `50_Knowledge/notes` | Atomic notes folder |
| `OBSIDIAN_KNOWLEDGE_MOC_DIR` | `50_Knowledge/_moc` | MOC index folder |
| `OBSIDIAN_DEFAULT_MOC` | `MOC-investment-analysis` | Primary MOC (auto-created) |

Set `OBSIDIAN_KNOWLEDGE_ENABLED=false` if you only want run reports, not `50_Knowledge/notes`.

### First-time steps

1. Pull/create container from template or `docker-compose.unraid.yml`.
2. `docker exec -it StockAnalyzer claude login` (or `stock-analyzer` — match container name).
3. Open dashboard → **Sync T212**.
4. **Run Analysis** or use **↻** on a single card.

### Verify Obsidian mount

```bash
docker exec -it StockAnalyzer ls "/obsidian/10_Personal/13_Finances/AI Investment Analysis"
docker exec -it StockAnalyzer ls "/obsidian/50_Knowledge/_moc"
```

---

## Proxmox setup

Use `docker-compose.proxmox.yml`. Bind-mount fast storage into the LXC, then set in `.env`:

```bash
PRIMARY_DATA_PATH=/opt/stock-analyzer
BACKUP_HOST_PATH=/mnt/backups/stock-analyzer
OBSIDIAN_VAULT_HOST_PATH=/mnt/obsidian/MyVault
OBSIDIAN_VAULT_DIR=/obsidian
OBSIDIAN_REPORTS_SUBDIR=10_Personal/13_Finances/AI Investment Analysis
OBSIDIAN_KNOWLEDGE_ENABLED=true
OBSIDIAN_KNOWLEDGE_SUBDIR=50_Knowledge/notes
OBSIDIAN_KNOWLEDGE_MOC_DIR=50_Knowledge/_moc
OBSIDIAN_DEFAULT_MOC=MOC-investment-analysis
```

LXC config example (paths are examples):

```
mp0: /path/on/host/stock-analyzer,mp=/opt/stock-analyzer
mp1: /path/on/host/MyVault,mp=/mnt/obsidian/MyVault
```

---

## Dashboard controls

| Control | Action | Claude calls | Reports |
|---------|--------|--------------|---------|
| **Run Analysis** | All positions + pies | One per holding | Excel + text + Obsidian run `.md` |
| **↻** on a card | That ticker/pie only | One | Obsidian run `.md` only (no Excel/text) |
| **Sync T212** | Refresh positions/prices | None | None |
| Scheduled run (Mon/Wed/Sat default) | Sync (if enabled) + full analysis | Same as full run | Same as full run |

Only one analysis can run at a time (full or single).

---

## What happens when you run analysis

### Per holding (stocks and pies)

1. **Resolve market ticker** — T212 code (e.g. `SOAC`) may map to yfinance symbol (e.g. `TMC`) by matching wallet price to public quotes.
2. **Fetch market data** — price, P/E, news headlines, analyst targets, earnings date.
3. **Merge T212 price** — position valuation uses Trading 212 wallet price when available.
4. **Load handoff memory** — short thesis from last run; stale “worthless / $0” memory is dropped if T212 shows a live price.
5. **Claude analysis** — JSON: recommendation, reasoning, catalysts, risks, outlook, handoff for next run.
6. **Knowledge notes** (optional) — if Claude returns non-empty `knowledge_notes`, write atomic `.md` files and update MOCs.
7. **Save to SQLite** — latest analysis per ticker; handoff note updated.

### When the full run finishes

| Output | Location |
|--------|----------|
| Dashboard cards | Updated after page reload |
| Database | `/data/db/stocks.db` — run history + per-ticker analysis |
| Excel report | `/data/reports/analysis_run{id}_{timestamp}.xlsx` |
| Text report | `/data/reports/analysis_run{id}_{timestamp}.txt` |
| Obsidian run note | `{OBSIDIAN_REPORTS_SUBDIR}/{date} Analysis Run {id}.md` |
| MOC link | `50_Knowledge/_moc/MOC-investment-analysis.md` → **## Analysis runs** |

Old files in `/data/reports` may be deleted after `REPORTS_RETENTION_DAYS` (default 365). Obsidian files are **never** auto-deleted.

### Single-card (↻) run

Same per-holding steps, but only one ticker. At the end:

- Obsidian run `.md` (that holding only).
- MOC link for that run.
- Knowledge notes if Claude included them.
- **No** Excel/text in `/data/reports`.

---

## T212 ticker vs market symbol (e.g. SOAC / TMC)

Trading 212 may keep a **legacy instrument code** after a SPAC rename or merger.

| UI label | Meaning |
|----------|---------|
| Card title | Company name from T212 / yfinance |
| `SOAC · quotes TMC` | DB key is `SOAC`; live quotes use `TMC` |

Analysis should use **TMC** fundamentals with **T212** position prices. If the card still shows old “SOAC worthless $0” text, run **↻** on that card after deploying ticker-resolution fixes and syncing.

---

## Obsidian: three types of output

### 1. Run reports (every successful run)

- **Folder:** `10_Personal/13_Finances/AI Investment Analysis`
- **Filename:** `YYYY-MM-DD Analysis Run {run_id}.md`
- **MOC:** Always linked under **## Analysis runs** on `MOC-investment-analysis`
- **Frontmatter:** `run_id`, `generated`, `tickers`, tags, link to default MOC

Contains one `## TICKER` section per holding in that run with thesis, news, catalysts, risks.

### 2. Knowledge notes (only when Claude adds them)

- **Folder:** `50_Knowledge/notes`
- **Filename:** `YYYYMMDDHHMMSS-{slug}.md` (updates existing file if same `slug`)
- **When:** Claude returns non-empty `knowledge_notes` — selective, not every run

**Typical reasons for a knowledge note:**

- Ticker mapping (T212 code vs market symbol)
- Durable sector/framework insight
- Material thesis change worth remembering
- Correcting a prior misconception

**Not written for:** routine HOLD/BUY recaps, price targets only, P&L summaries.

**MOC linking for knowledge notes:**

- Always: `MOC-investment-analysis` → **## Knowledge notes**
- Additionally: any MOC Claude lists (e.g. `MOC-uk-reits`) — file created if missing

### 3. MOC files (index)

- **Folder:** `50_Knowledge/_moc`
- **Default:** `MOC-investment-analysis.md` (auto-created with **Analysis runs** and **Knowledge notes** sections)
- **Topic MOCs:** Created/updated only when Claude names them on a knowledge note

Run reports are **not** auto-linked to topic MOCs like `MOC-uk-reits` — only to `MOC-investment-analysis`.

---

## Handoff memory (between runs)

Stored in SQLite per ticker (`handoff_notes` table), not as separate Obsidian files.

Includes: thesis one-liner, watch items, trend flags, ongoing risks/catalysts.

Used on the **next** analysis for that ticker unless marked stale (e.g. “worthless” while T212 shows a positive price).

---

## Schedule and sync

| Setting | Default | Meaning |
|---------|---------|---------|
| `SCHEDULE_ENABLED` | `true` | Automatic runs |
| `SCHEDULE_DAYS` | `0,2,5` | Mon, Wed, Sat |
| `SCHEDULE_HOUR` | `3` | Hour in `TZ` |
| `T212_SYNC_ENABLED` | `true` | Sync before scheduled analysis |

Manual **Sync T212** and **Run Analysis** always work regardless of schedule.

---

## Security notes

| Setting | Effect |
|---------|--------|
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | Basic auth on dashboard |
| `TRUSTED_NETWORKS` | IPs that skip basic auth (e.g. behind Authelia) |
| `REPORTS_ENCRYPTION_KEY` | Password-protects Excel; encrypts `.txt` reports |

Use a strong dashboard password. Obsidian vault on the host should have normal file permissions (only your user/containers that need access).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| No `.md` in Obsidian | Old image, or vault not mounted | Deploy latest image; check `/obsidian` mount + env vars |
| `ls /obsidian/...` fails | Wrong host path (not vault root) | Mount folder containing `10_Personal` and `50_Knowledge` |
| Still “SOAC $0” on card | Stale analysis in DB | Sync T212 → **↻** on card |
| `SOAC · quotes TMC` but wrong analysis | Expected until re-analysed | **↻** after sync |
| No knowledge notes | Normal — most runs return `[]` | Only when Claude flags durable insight |
| No `MOC-uk-reits` link | Claude didn’t list it on a knowledge note | Add `mocs` in prompt output or link manually |
| Analysis already running | Overlap | Wait for current run to finish |
| Quota / auth error | Claude limits or not logged in | `docker exec -it … claude login`; wait for quota |
| Watchtower didn’t add Obsidian path | Watchtower only updates image | Edit container once to add path + env |

---

## Environment variable reference (complete)

### Core

| Variable | Default |
|----------|---------|
| `DB_PATH` | `/data/db/stocks.db` |
| `REPORTS_DIR` | `/data/reports` |
| `EXCEL_PATH` | `/data/stocks/stocks.xlsx` |
| `LOG_DIR` | `/data/logs` |
| `PORT` | `8765` |
| `COST_METHOD` | `AVCO` |

### Reports

| Variable | Default |
|----------|---------|
| `REPORTS_ENCRYPTION_KEY` | (empty) |
| `REPORTS_RETENTION_DAYS` | `365` |

### Obsidian

| Variable | Default |
|----------|---------|
| `OBSIDIAN_VAULT_DIR` | (empty — disabled) |
| `OBSIDIAN_REPORTS_SUBDIR` | `10_Personal/13_Finances/AI Investment Analysis` |
| `OBSIDIAN_KNOWLEDGE_ENABLED` | `true` |
| `OBSIDIAN_KNOWLEDGE_SUBDIR` | `50_Knowledge/notes` |
| `OBSIDIAN_KNOWLEDGE_MOC_DIR` | `50_Knowledge/_moc` |
| `OBSIDIAN_DEFAULT_MOC` | `MOC-investment-analysis` |

### Compose host paths (not inside container)

| Variable | Used by |
|----------|---------|
| `ZFS_APPDATA_PATH` | `docker-compose.unraid.yml` |
| `OBSIDIAN_VAULT_HOST_PATH` | Unraid/Proxmox compose |
| `PRIMARY_DATA_PATH` | Proxmox compose |
| `BACKUP_HOST_PATH` | Proxmox compose |

---

## Suggested place in your vault

Copy this file to something like:

```text
Home/
  Apps/
    Stock Analyser.md    ← this guide
```

Link it from a homelab or finance MOC if you use one, e.g. `[[Stock Analyser]]` under `MOC-homelab` or `MOC-investment-analysis`.

---

## Related notes in this vault

After the app runs, you will see generated content here (not shipped with the repo):

- `[[MOC-investment-analysis]]` — index of runs and knowledge notes
- `10_Personal/13_Finances/AI Investment Analysis/` — dated run reports
- `50_Knowledge/notes/` — atomic notes from the analyser (when created)

---

*Last updated for features: per-card ↻ analysis, Obsidian run reports, knowledge notes, auto MOC create/link.*
