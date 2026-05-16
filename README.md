# Stock Analyzer — AI-Powered Portfolio Analysis

An AI-powered stock portfolio analyser running as a Docker container.
Reads holdings from Trading 212 (or an Excel file), fetches live market data,
and uses the Claude API to generate Buy/Hold/Sell recommendations with 30-day
price targets, news sentiment, catalysts, risks, and a 90-day outlook.

Designed to run on Unraid or Proxmox LXC with automatic nightly backups and
Watchtower auto-updates via GHCR.

---

## Quick start (local / dev)

```bash
# Authenticate with your Claude subscription (one-time)
claude login

cp .env.example .env
# Edit .env — add TRADING212_API_KEY if you want T212 sync

mkdir -p data/db data/reports data/stocks
docker compose up --build
```

Open **http://localhost:8765** and click **Run Analysis Now**.

---

## Unraid setup

### Option A — Docker form UI (recommended)

1. **Authenticate to Claude** — open Unraid terminal (Tools → Terminal) and run:
   ```bash
   claude login
   ```
   This links the container to your Claude Pro/Max subscription. Credentials are saved at `/root/.claude` and mounted into the container read-only — no API key needed.

2. **Authenticate to GHCR** — still in the terminal:
   ```bash
   docker login ghcr.io -u tayoos
   ```
   When prompted for a password, use a GitHub Personal Access Token with `read:packages` scope
   (GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)).

3. **Load the template** — run this once in the terminal to get the pre-filled form:
   ```bash
   mkdir -p /boot/config/plugins/dockerMan/templates-user && \
   curl -H "Authorization: token YOUR_GITHUB_PAT" \
        -H "Accept: application/vnd.github.v3.raw" \
        -L "https://api.github.com/repos/tayoos/Trading-Analysis---AI-Assisted/contents/unraid-template.xml" \
        -o /boot/config/plugins/dockerMan/templates-user/StockAnalyzer.xml
   ```
   Replace `YOUR_GITHUB_PAT` with the same token used above.

4. **Add the container** — Docker → Add Container → select **StockAnalyzer** from the Template dropdown.
   All fields pre-fill. Review and adjust the volume paths to match your pool name, then click Apply.

### Option B — Manual form entry

Docker → Add Container and fill in the following. Use **"Add another Path, Port, Variable"** for each row.

**Top fields**

| Field | Value |
|---|---|
| Name | `StockAnalyzer` |
| Repository | `ghcr.io/tayoos/trading-analysis---ai-assisted:latest` |
| Network Type | Bridge |

**Ports**

| Name | Container Port | Host Port |
|---|---|---|
| Web UI | `8765` | `8765` |

**Paths** — adjust host paths to match your pool/share names

| Name | Container path | Host path |
|---|---|---|
| Database | `/data/db` | `/mnt/cache/appdata/stock-analyzer/db` |
| Reports | `/data/reports` | `/mnt/cache/appdata/stock-analyzer/reports` |
| Stocks input | `/data/stocks` | `/mnt/cache/appdata/stock-analyzer/stocks` |
| Backups | `/data/backups` | `/mnt/user/appdata/stock-analyzer/backups` |

> The first three paths should live on your **fast ZFS/cache pool** for best performance.
> The Backups path should point to your **main Unraid array** (`/mnt/user/...`) for parity protection.
> Mounting the Backups volume enables nightly backups automatically. Omit it to disable backups.

**Variables**

**Claude credentials path** (Type = Path)

| Name | Container path | Host path |
|---|---|---|
| Claude credentials | `/home/appuser/.claude` | `/root/.claude` |

> Run `claude login` in the Unraid terminal first — this creates `/root/.claude` with your subscription credentials.

**Variables**

| Name | Key | Value |
|---|---|---|
| T212 Key | `TRADING212_API_KEY` | your T212 read-only API key |
| Dashboard Username | `DASHBOARD_USER` | e.g. `admin` |
| Dashboard Password | `DASHBOARD_PASSWORD` | strong password |
| Trusted Networks | `TRUSTED_NETWORKS` | `127.0.0.1/32,::1/128` — add your proxy/LAN subnet if using Authelia |
| Backup Retention | `BACKUP_RETAIN_DAYS` | `60` |

> If you use **Authelia** or another reverse proxy, add the proxy's IP/subnet to `TRUSTED_NETWORKS`
> (e.g. `127.0.0.1/32,192.168.1.0/24`) so users aren't prompted to log in twice.

### Create directories before first start

```bash
mkdir -p /mnt/cache/appdata/stock-analyzer/{db,reports,stocks}
mkdir -p /mnt/user/appdata/stock-analyzer/backups
```

Replace `cache` with your actual ZFS pool name.

### After first start

Visit **http://[UnraidIP]:8765/sync** and click **"Mark as rotated"** for both API keys
to start the rotation reminder clock.

---

## Proxmox LXC setup

1. Add bind mounts to your LXC config (`/etc/pve/lxc/<id>.conf`):
   ```
   mp0: /mnt/pve/nvme-pool/stock-analyzer,mp=/opt/stock-analyzer
   mp1: /mnt/pve/backup-pool/stock-analyzer,mp=/mnt/backups/stock-analyzer
   ```
   Adjust paths to match your Proxmox storage pools. `mp1` can be an NFS share or secondary disk.

2. Inside the LXC, authenticate to GHCR:
   ```bash
   docker login ghcr.io -u tayoos
   ```

3. Copy the compose file and configure:
   ```bash
   cp docker-compose.proxmox.yml docker-compose.yml
   cp .env.example .env
   # Edit .env with your keys and adjust PRIMARY_DATA_PATH / BACKUP_HOST_PATH
   docker compose up -d
   ```

---

## Watchtower auto-updates

Both compose files include a Watchtower service that polls GHCR every 5 minutes
and restarts the container automatically when a new image is pushed.

Push to `main` → GitHub Actions builds and pushes `ghcr.io/tayoos/trading-analysis---ai-assisted:latest`
→ Watchtower pulls and restarts the container.

---

## Portfolio sources

| Source | Setup | Priority |
|--------|-------|----------|
| Trading 212 API | Set `TRADING212_API_KEY` | Primary — auto-syncs trades and dividends |
| Excel fallback | `stocks.xlsx` in `/data/stocks/` with Ticker / Shares / Buy Price columns | Used when no DB positions exist |

---

## Environment variables

See `.env.example` for the full annotated list. Key variables:

| Variable | Default | Notes |
|----------|---------|-------|
| `TRADING212_API_KEY` | — | Optional; enables T212 sync |
| `DASHBOARD_USER` | — | Basic Auth username |
| `DASHBOARD_PASSWORD` | — | Basic Auth password |
| `TRUSTED_NETWORKS` | — | CIDRs that bypass Basic Auth (proxy/Authelia) |
| `REPORTS_ENCRYPTION_KEY` | — | Encrypts Excel reports + text files if set |
| `REPORTS_RETENTION_DAYS` | `365` | Auto-delete reports older than this |
| `BACKUP_RETAIN_DAYS` | `60` | Auto-delete backups older than this |
| `SCHEDULE_DAYS` | `0,2,5` | Days to run analysis (0=Mon…6=Sun) |
| `SCHEDULE_HOUR` | `7` | UTC hour for scheduled runs |
| `COST_METHOD` | `AVCO` | `AVCO` or `FIFO` |

---

## Architecture

```
Trading 212 API  ──→  sources/t212.py  ──→  portfolio.py (AVCO)
                                                  ↓
                                            SQLite (WAL mode)
                                                  ↓
Excel fallback   ──→  portfolio.py       analyzer.py (Claude API)
                                                  ↓
                                            reports.py (Excel + txt)
                                                  ↓
                                         Flask web UI (port 8765)
```

### Handoff memory

Each Claude call returns a `handoff_note` (thesis, watch items, trend flags, risks, catalysts)
stored in SQLite. The next run injects it into the prompt (~150 tokens/ticker), giving the model
continuity without replaying full history.

### Backups

The `BackupManager` uses SQLite's `Connection.backup()` API — safe under concurrent writes with WAL mode.
Reports are copied alongside the database. Mount a volume at `/data/backups` to enable nightly backups
at 02:00 UTC; the Sync page also has a manual trigger.

### Adding a new data source (e.g. crypto)

1. Create `app/sources/myexchange.py` implementing `DataSource` from `app/sources/base.py`
2. Register it in `create_app()` in `app/__init__.py`
3. Wire it into `portfolio.py` as an additional source

---

## Project structure

```
app/
├── __init__.py          Flask app factory + APScheduler
├── analyzer.py          Claude analysis engine + handoff notes
├── backup.py            SQLite hot-backup + report copy + rotation
├── database.py          SQLite layer (WAL, all tables)
├── portfolio.py         AVCO cost basis + Excel reader
├── ratelimit.py         Sliding-window in-memory rate limiting
├── reports.py           Excel + text report generation (optional encryption)
├── sources/
│   ├── base.py          DataSource ABC (extend for new brokers/exchanges)
│   └── t212.py          Trading 212 REST API client
└── routes/
    ├── dashboard.py     GET /
    ├── analysis.py      POST /api/run, GET /api/status
    ├── history.py       GET /history, /ticker/<t>
    └── sync.py          T212 sync, backup, key rotation endpoints
templates/
├── dashboard.html       Main portfolio view with sparklines + key warnings
├── history.html         Run history
├── ticker.html          Per-ticker detail + handoff memory viewer
└── sync.html            T212 sync, closed positions, dividends, key rotation, backups
docker-compose.yml             Local / dev
docker-compose.unraid.yml      Unraid (ZFS pool + main array backup)
docker-compose.proxmox.yml     Proxmox LXC (NVMe bind-mount + NFS backup)
unraid-template.xml            Community Applications template
```
