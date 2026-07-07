# Fire & Rescue Academy Discord Bot

Standalone Discord bot for the **Fire & Rescue Academy** MissionChief USA
alliance. It logs in to [missionchief.com](https://www.missionchief.com),
collects alliance data at a human pace, stores it robustly in SQLite and
surfaces it in Discord.

Designed to run 24/7 on a **Raspberry Pi 4B with Debian Bookworm**
(Python 3.11), but runs anywhere Python 3.11+ is available.

## What it does (phase 1 — data foundation)

| Data | Source | Schedule | Discord output |
|---|---|---|---|
| Members (name, role, earned credits, contribution rate, member since) | `/verband/mitglieder/<id>` (47+ pages) | hourly | join/leave/role/contribution change events |
| Applications | `/verband/bewerbungen` | every 5 min | new-application alert |
| Alliance logs | `/alliance_logfiles` | every 15 min | log feed (classified, colour-coded) |
| Alliance funds + income top lists | `/verband/kasse` (+`?type=monthly`) | every 30 min + a final capture at 23:52 New York | daily & monthly top-10 reports after the New York midnight reset |
| Expense ledger (3150+ pages) | `/verband/kasse?page=N` | one resumable backfill, then incremental | — (stored for reporting/auditing) |

Browser emulation (building placement, event starts) is a later phase and
will build on this data foundation.

## Design principles

* **Store first, then act.** Every scraper writes to SQLite (WAL mode,
  versioned migrations, transactions); Discord posting is driven by
  `posted_at IS NULL` rows, so restarts/crashes can never skip or
  mass-repeat announcements.
* **Human-like traffic.** One global pacer for all MissionChief requests:
  4–9 s randomized delay between requests, a hard requests-per-minute cap,
  `Retry-After`-aware backoff and a circuit breaker that pauses all
  scraping after repeated failures. Sessions are persisted to disk, so a
  restart does not re-login.
* **Fail loud, not wrong.** A page that doesn't look like what we expect
  aborts the run *without touching the database* — a truncated member list
  is never interpreted as mass departures (retention guard), and layout
  changes surface in the admin channel instead of storing garbage.
* **Look-alike rows are real.** The expense ledger and alliance logs
  legitimately contain identical-looking rows. Deduplication is done on
  row *sequences* (anchor matching) and occurrence indexes, never by
  dropping "duplicates".
* **Timezones handled once.** Storage is UTC everywhere; the MissionChief
  game day/month is America/New_York; income snapshots are keyed by the NY
  game period, so the midnight reset can't corrupt reports.

## Installation (Raspberry Pi 4B, Debian Bookworm)

### Quick install (recommended)

One command — it installs system packages, sets up the virtualenv, asks
for your Discord token / MissionChief login / channel ids, installs the
systemd service and starts the bot:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/install.sh)
```

Until this branch is merged to `main`, install from the branch instead:

```bash
FRA_BRANCH=claude/fra-discord-bot-ggwg15 bash <(curl -fsSL https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/claude/fra-discord-bot-ggwg15/install.sh)
```

Useful afterwards:

```bash
journalctl -u fra-bot -f          # follow the logs
sudo systemctl restart fra-bot    # restart (e.g. after editing config.yaml)
./install.sh                      # run again = update to the latest version
./install.sh uninstall            # remove the service (keeps data & config)
```

The installer is idempotent: re-running it updates the code and
dependencies but never overwrites your existing `config.yaml` or `.env`.

### Manual install

```bash
sudo apt update && sudo apt install -y python3 python3-venv git

git clone https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot.git
cd FireAndRescueAcademyDiscordBot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Configuration
cp config.example.yaml config.yaml   # edit: alliance id, channel ids, intervals
cp .env.example .env                 # edit: DISCORD_TOKEN, MC_EMAIL, MC_PASSWORD
chmod 600 .env

# First run (foreground, to check the logs)
.venv/bin/python -m fra_bot
```

Then install the systemd service so it survives reboots:

```bash
sudo cp deploy/fra-bot.service /etc/systemd/system/   # adjust User/paths first
sudo systemctl daemon-reload
sudo systemctl enable --now fra-bot
journalctl -u fra-bot -f
```

### Discord setup

1. Create an application + bot at <https://discord.com/developers>,
   enable the **Server Members** and **Message Content** intents.
2. Put the token in `.env`.
3. Fill the channel ids in `config.yaml` (`admin_log`, `applications`,
   `member_events`, `alliance_logs`, `reports`). Set a channel to `0` to
   disable that output.

### First start behaviour

* The first members sync stores the roster silently (no 47 pages of
  "member joined" spam). Change detection starts from the second run.
* The first logs sync backfills 25 pages and marks them as already
  posted, so history doesn't flood the feed.
* The expense backfill walks all 3150+ pages in chunks of 30 pages every
  15 minutes (roughly a day and a half); progress is stored and resumes
  automatically after a restart. Incremental expense sync takes over
  once the backfill completes. Check progress with `!fra status`.

## Admin commands

Requires Discord administrator permission or a role listed in
`discord.admin_role_ids`.

| Command | Description |
|---|---|
| `!fra status` | Data counts, backfill progress, circuit breaker, recent sync runs |
| `!fra sync <members\|applications\|logs\|treasury\|expenses\|backfill>` | Run a sync now |
| `!fra balance` | Latest known alliance funds |
| `!fra top10 [daily\|monthly]` | Current income top-10 |
| `!fra report [daily\|monthly]` | Repost the last completed period's report |

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Project layout:

```
fra_bot/
├── config.py            # YAML + .env configuration
├── bot.py               # discord.py bot, job scheduling
├── core/                # pacing (rate limits), scheduler
├── mc/                  # MissionChief client (login, cookies, retries)
│   └── parsers/         # one defensive parser per page type
├── db/                  # aiosqlite + migrations + repositories
│   └── migrations/      # numbered .sql files
├── services/            # sync services (fetch → parse → store)
└── cogs/                # Discord layer (publisher, reports, admin)
```
