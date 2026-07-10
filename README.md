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

## What it does (phase 2 — board request automation)

The bot watches the alliance board threads and acts on member requests.
**Everything is off by default and starts in dry-run** (detect + report,
no MissionChief actions) so you can watch it before letting it act.

| Request | Thread | What the bot does |
|---|---|---|
| Trainings | 5935 | Matches the requested course, checks the requester's contribution rate, opens a free 1-hour alliance class in the right academy, verifies a classroom was actually taken, and replies |
| Hospitals / prisons | 6165 | Geocodes the shared Google Maps link, detects hospital vs prison, checks the live alliance-funds floor, builds via browser emulation (optional Playwright), and replies |
| Events | 15293 | Geocodes the location and starts a large scale alliance mission there as soon as the free-start cooldown allows (queued otherwise) |

Enable per feature in `config.yaml` under `automation:` and flip
`dry_run: false` once you trust it. `!fra automation` shows the current
switches and recent requests.

### Optional: browser emulation for building placement

Building placement drives MissionChief's JS form with Playwright. It is
**optional** — without it, building requests are geocoded and reported
for an admin to place manually. To enable automatic building on the Pi:

```bash
cd ~/FireAndRescueAcademyDiscordBot
.venv/bin/pip install playwright
.venv/bin/python -m playwright install --with-deps chromium
```

Chromium adds ~400 MB and needs memory; on a Pi 4B keep other load low.
Trainings and events use plain HTTP and need no browser.

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

One command **over SSH on the Pi** — it installs system packages, sets
up the virtualenv, asks for your Discord token / MissionChief login /
channel ids, installs the systemd service and starts the bot:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/install.sh)"
```

Until this branch is merged to `main`, install from the branch instead:

```bash
FRA_BRANCH=claude/fra-discord-bot-ggwg15 bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/claude/fra-discord-bot-ggwg15/install.sh)"
```

### Non-interactive install (no terminal)

Running the installer where no terminal is available — for example via
another bot's shell command — requires passing the answers as
environment variables (and the user must have passwordless sudo, which
is the Raspberry Pi OS default for the `pi` user):

```bash
FRA_BRANCH=claude/fra-discord-bot-ggwg15 \
FRA_DISCORD_TOKEN=your-discord-token \
FRA_MC_EMAIL=you@example.com \
FRA_MC_PASSWORD=your-mc-password \
FRA_GUILD_ID=123 FRA_CH_ADMIN=123 FRA_CH_APPS=123 \
FRA_CH_MEMBERS=123 FRA_CH_LOGS=123 FRA_CH_REPORTS=123 \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/claude/fra-discord-bot-ggwg15/install.sh)"
```

All `FRA_CH_*`/`FRA_GUILD_ID` variables are optional (default 0 =
disabled; edit `config.yaml` later). Note: `bash <(curl ...)` does NOT
work in `/bin/sh` — use the `bash -c "$(curl ...)"` form above.

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

### Geocoding (optional API key)

Address/place lookups default to free OSM Nominatim — no key needed, and
Google Maps links never need one (coordinates are read from the URL). To
use your own quota, point `geocoding.base_url` at a Nominatim-compatible
provider and put the key in `.env` as `GEOCODER_API_KEY`:

```yaml
# config.yaml
geocoding:
  base_url: https://geocode.maps.co     # maps.co
  api_key_param: api_key                # 'key' for LocationIQ
```
```bash
# .env
GEOCODER_API_KEY=your-key
```

### First start behaviour

* The first members sync stores the roster silently (no 47 pages of
  "member joined" spam). Change detection starts from the second run.
* The first logs sync backfills 25 pages and marks them as already
  posted, so history doesn't flood the feed.
* The expense backfill walks all 3150+ pages in chunks of 30 pages every
  15 minutes (roughly a day and a half); progress is stored and resumes
  automatically after a restart. Incremental expense sync takes over
  once the backfill completes. Check progress with `!fra status`.

## Live bot status

The bot's Discord presence reflects what it's doing right now, so you
can tell at a glance that it's alive:

* `🔄 syncing members…` / `checking applications…` / etc. while a job runs
* `👀 47 members · 3 applications · backfill p120` when idle
* `⚠️ paused (MissionChief cooldown)` (do-not-disturb) if the circuit
  breaker has paused scraping after repeated failures

Updates are driven by a background reconciler that stays well under
Discord's presence rate limit.

## Admin commands

Requires Discord administrator permission or a role listed in
`discord.admin_role_ids`.

| Command | Description |
|---|---|
| `!fra status` | Data counts, backfill progress, circuit breaker, recent sync runs |
| `!fra sync <members\|applications\|logs\|treasury\|expenses\|backfill>` | Run a sync now |
| `!fra synccommands [global]` | Re-sync the slash commands with Discord (guild by default; rate-limited to once/10 min) |
| `!fra balance` | Latest known alliance funds |
| `!fra top10 [daily\|monthly]` | Current income top-10 |
| `!fra report list` | List every registered report and its periods |
| `!fra report <name> [period]` | Render any report (`period`: today/yesterday/week/month/prev-month/year/prev-year/all; `daily`/`monthly` alias the income top-10) |
| `!fra automation` | Board automation switches, dry-run state, recent requests |
| `!fra sync <trainings\|buildings\|events>` | Poll a board thread now |
| `!fra sync missions` | Advance the mission/event queue + rotation now |
| `!fra missionpanel` | Post the "Request a mission" panel to the configured channel |
| `!fra missions [limit]` | List recent scheduled missions and their status |
| `!fra cancelmission <id>` | Cancel a not-yet-started scheduled mission |
| `!fra deletemission <id\|all>` | Delete a mission row (any status); `all` clears finished ones |
| `!fra coinmission <location> [\| …] [\| confirm]` | Owner-only: start using **coins** (ignores the free cooldown). Previews unless `\| confirm` |
| `!fra nextmission` | Show which mission/event is up next and where (also shown in eventpinger pings) |
| `!fra rotation` / `list` | Show the auto-start rotation list and which entry is next |
| `!fra rotation add <location> [\| kind: event] [\| preset: Pile-up] [\| custom: need_lf=25 …] [\| saved: <name>] [\| name: <caption>]` | Add a location to the rotation |
| `!fra rotation remove\|on\|off <id>` | Remove, pause or resume a rotation entry |
| `!fra testbuild <hospital\|prison> <location>` | Test the building flow for a location (dry-run drives the form without submitting) |
| `!fra dailybuild` | Run the daily worldwide auto-build now (works with the schedule off; dry-run only reports) |
| `!fra dump <path> [rendered]` | Upload a MissionChief page's HTML for inspection (CSRF tokens redacted; `rendered` runs it through Playwright) |
| `!fra update` | Pull the latest code, install deps and restart the bot |
| `!fra restart` | Restart the bot to reload `config.yaml` / `.env` (no code update) |

Members request missions with the **/mission** slash command or the panel
button (see below).

### Missions & events

One system handles every mission/event request. A request has four choices:

* **location** — a place name ("Grand Rapids", "Wal Amsterdam") or a maps
  link; it's geocoded;
* **kind** — an alliance **event** or a **large** scale alliance mission;
* **large-mission data** — a **preset**, a **custom** Own mission (you supply
  the required-unit values, e.g. `need_lf=25 need_elw1=6 water_needed=15000`;
  each field caps at 100 except water/foam at 1,000,000), or one of the
  game's **saved** missions picked by name;
* **event options** — the event **type** (Storm, Civil Unrest, Storm Surge,
  Fall/Winter/Spring/Summer weather, Sports Event, or **Random** — which picks
  a standard one and skips seasonal currency events like Soccer Game), plus
  **Area** (small/medium/large), **Shape** (rectangle/circle) and **Call
  volume** (30/45/60 s);
* **schedule** — one-time, or **recurring** (it joins the rotation list).

Requests arrive three ways:

* the **/mission** slash command (with `kind` / `schedule` / `preset` /
  `saved` / `custom` / event options), or the button on the panel posted by
  `!fra missionpanel` (channel set by `automation.mission.panel_channel_id`);
* a **board post** — just a location, no command word needed. Each request
  board has a default kind: the **events board** (`automation.events.thread_id`,
  enabled by `automation.events.enabled`) starts an alliance **event**; the
  **mission board** (`automation.mission.thread_id`, enabled by
  `automation.mission.board_enabled`) starts a **large scale mission**. A
  member just posts e.g. `New York City`, `Amsterdam, Netherlands`, or a maps
  link. Optional refinement lines fine-tune it — events: `event: Storm`,
  `area: large`, `shape: circle`, `call: 30`; missions: `custom: need_lf=25
  need_elw1=6`, `saved: <name>`, `name: <caption>`; either: `schedule:
  recurring`. The bot maintains a how-to-request guide post on each board,
  confirms accepted requests, and asks for clarification when a post can't be
  used. Event posts with no refinements default to a random type at
  Large / Circle / 30s.

The **rotation list** is an admin-managed set of locations the bot starts
automatically and keeps cycling forever — one per free slot, oldest-started
first. **Member requests come first**; the rotation only fills a free slot
when nothing is queued. `!fra nextmission` reports which mission/event is up
next and where. Recurring member requests are promoted into the rotation
once they first start.

Everything queues in `scheduled_missions` and starts **one at a time at the
next free slot** (cooldown-aware), with a hard free-only guard — the bot
never spends coins. The scheduler only runs when `automation.mission.enabled`,
and a real start only happens when `automation.dry_run` is off; in dry-run
each request is recorded as *skipped* with what would have been started.
Outcomes are announced back in Discord (never on MissionChief while in
dry-run). The `missions` report (`!fra report missions`) summarises requests
and outcomes.

MissionChief silently refuses a start while another alliance mission/event
is still running (the POST is accepted but no mission appears). The bot
treats that as *waiting*, not a failure: the request stays queued, all free
starts back off for an hour (the bot knows what it started, so it doesn't
hammer doomed attempts), and only a request that keeps being refused for
48 hours is surfaced as failed. Owner coin starts bypass the backoff.

### Built-in eventpinger

Because the bot starts every alliance mission/event itself, it doesn't need
to watch a channel for MissionChief announcements. Each **confirmed** start
is recorded in an outbox and posted as a role ping in
`discord.channels.event_pings`: always the Notify-Event role
(`discord.notify_event_role_id`), plus the region role when the mission's
coordinates resolve to one — roles named like `Michigan (MI)` /
`Bermuda (BM)` / `Germany (DE)`, using the old eventpinger cog's logic
(reverse geocode first, then US state names, ZIP ranges, place aliases and
Bermuda postal codes as text fallbacks). The embed shows what started,
where, the region, and the next queued/rotation location with its expected
start time. Dry-run starts never ping; stale pings (>24h) are dropped.
Both settings can be changed live with `!fra set`.

### Updating from Discord

`!fra update` fetches the latest code on the bot's branch, installs any
new dependencies into its virtualenv, and restarts in place (no SSH, no
sudo needed). It reports the changelog and restarts within ~15 seconds.
If there's nothing new it just says "Already up to date". Configuration
(`config.yaml`, `.env`) and the database are never touched. You can
still update over SSH by re-running `install.sh`.

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
├── geo/                 # Google Maps link parsing + geocoding
├── services/            # sync + board automation services
└── cogs/                # Discord layer (publisher, reports, admin, automation)
```
