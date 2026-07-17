---
name: run-fra-discord-bot
description: Run, test, smoke-test and develop the FRA Discord bot — offline driver for the service layer, full pytest suite, real-bot boot, and the PR/merge/branch workflow with this repo's hard-won gotchas (rollbacks, reference-cog porting, live-settings traps).
---

# Run & develop the FRA Discord bot

Headless discord.py bot for a MissionChief USA alliance. Its two
external surfaces (Discord, missionchief.com) are **unreachable from
the dev container**, so the agent path is the offline driver: it runs
config loading, all migrations, and real end-to-end service flows
(training request → class opened, application → auto-accept, FAQ
fuzzy search, chat parsers, settings registry) against canned HTML.
That is the layer every PR here touches. All paths below are relative
to the repo root.

## Prerequisites

Python 3.11 venv with both requirements files (already present as
`.venv/` in this container; recreate with):

```bash
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
```

## Run (agent path): the offline smoke driver

```bash
.venv/bin/python .claude/skills/run-fra-discord-bot/driver.py
```

Expected output (exit 0):

```
  OK  config        alliance 1621, dry_run=True
  OK  migrations    28 tables migrated
  OK  training      request #1 -> done (HazMat: opened)
  OK  applications  application 77 accepted and resolved
  OK  faq           'arr setup' -> FAQ #1 (score 81)
  OK  chat          form ok, 1 chat message(s) parsed
  OK  settings      automation.training.enabled <- 'on' parsed to True
driver: ALL FLOWS PASSED
```

One flow only: `--flow training` (also: config, migrations,
applications, faq, chat, settings). The stderr line
`could not parse /api/buildings … using list scrape` during the
training flow is EXPECTED — it is the alliance-list fallback working.

## Test

```bash
.venv/bin/python -m pytest tests/ -q
```

~800 tests, ~30 s, no network. **Green suite is the merge gate** —
this repo has no CI.

## Run (human path): the real bot

`python -m fra_bot [config.yaml]` — needs a real `DISCORD_TOKEN`,
`MC_EMAIL`, `MC_PASSWORD` and a `config.yaml` (copy
`config.example.yaml`). In this container it boots config + logging
and then dies at Discord login with
`403 … Host not in allowlist: discord.com` — that is the sandbox
egress, not a bug. The live deployment self-updates from `main` via
`!fra update`.

## Development workflow (how changes ship here)

1. Develop on `claude/…` branch; **never push to main**.
2. Before pushing: full suite green + run the driver.
3. Push, open the PR, then **squash-merge it yourself** (no CI; the
   local green suite is the gate), then reset the dev branch:
   `git fetch origin main && git checkout -B <branch> origin/main &&
   git push -u origin <branch> --force-with-lease`.
4. Porting from the reference bot (`Brandjuh/FireAndRescueAcademyCogs`,
   cloned at `/workspace/fireandrescueacademycogs` when added to the
   session): **treat every reference behaviour as load-bearing** —
   oddities encode real limits (e.g. archiving forum posts right after
   writing guards Discord's 1000-active-threads cap). Drop one only
   with a named reason, or ask the owner.
5. Every game action must honour `automation.dry_run` and go through
   `MissionChiefClient` (pacing, retries, re-login, error taxonomy).
   New autonomous credit-spending needs its own opt-in switch,
   default OFF, read LIVE each pass — never gate job registration on
   the switch (see gotchas).

## Gotchas (all hit for real in this repo)

- **The container rolls back the checkout.** Files that existed
  suddenly 404 ("File does not exist") and `git log` shows an old
  commit. Recover with:
  `git fetch origin main && git reset --hard && git clean -fd fra_bot
  && git checkout -B <branch> origin/main && pip install -q -r
  requirements.txt -r requirements-dev.txt`. Untracked files blocking
  the checkout: move them to the scratchpad first.
- **Stop-hook "Unverified commit" warnings are false positives**
  (GitHub squash commits + sandbox signing). NEVER `--reset-author`
  rebase published history over them.
- **`.env` is found next to the SOURCE TREE, not your cwd** —
  `load_dotenv()` walks up from `fra_bot/config.py`. Running the bot
  from another directory with a local `.env` there silently ignores
  it; export env vars instead.
- **Scheduler jobs are registered at startup** — a switch consumers
  read from `cfg` is live (`!fra set` mutates cfg in memory even for
  restart-required settings!), but a JOB gated at registration does
  not exist until restart. Register jobs unconditionally and gate
  inside the callback on the live cfg value.
- **`/api/buildings` lists only the BOT ACCOUNT's own buildings** —
  never the whole alliance. Alliance-wide enumeration needs
  `/verband/gebauede`. Union both (see `_find_academies`).
- **`parse_api_buildings` drops records without lat/lon by default**
  — pass `require_coordinates=False` when enumerating by type-id.
- **Academy pages can carry TWO education forms** (single-user first,
  alliance second). Take the LAST — posting to the first starts a
  personal course.
- **The free-start BUTTON is the truth, not computed timestamps** — a
  closed window is a wait (5-min then 30-min ladder), never a
  failure, and nothing may jump the list order (see missions.py).
- **`CircuitOpenError` is a `MissionChiefError`** on purpose — treat
  breaker trips as retry-later everywhere; a breaker trip during a
  POST that never sent is retry-SAFE (`busy`), an ambiguous submit
  error is not (`uncertain`, never re-post).
- **POSTs are actions**: one attempt, no blind retry. Timeouts wrap
  into `FetchError` so lost-response verification fires (cooldown
  advance, alliance-log confirmation).
- **Discord hard limits bite over time, not in tests**: 1000 active
  threads/guild (archive forum posts immediately after writing), 25
  options per select (paginate), 20 forum tags, 6000-char embeds.
- **missionchief.com is unreachable from the container** — anything
  about live pages is an estimate; label it as such or ask the owner.

## Troubleshooting

- `No module named fra_bot` running the driver from elsewhere → the
  driver self-inserts the repo root; run it with the repo venv python.
- `Configuration error: Environment variable MC_EMAIL is not set`
  while a `.env` sits in your cwd → see the `.env` gotcha above.
- `403 … Host not in allowlist: discord.com` on `python -m fra_bot`
  → expected in the container; the bot cannot log in here.
- Driver's `could not parse /api/buildings … using list scrape` on
  stderr → expected fallback, not a failure.
- Tests failing with `AttributeError: … SimpleNamespace … enabled`
  after adding a cfg field → test fixtures build cfg by hand; add the
  new field to the `SimpleNamespace` fixtures the failing tests use.
