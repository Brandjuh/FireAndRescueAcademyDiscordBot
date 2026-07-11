# FireAndRescueAcademyDiscordBot — agent notes

## Workflow

- After pushing a branch and opening a PR: **merge the PR yourself** once the
  full test suite is green — don't wait for the user to merge. Use a
  **squash merge** so main keeps its one-commit-per-PR history.
- Develop on the designated `claude/...` branch; never push to `main`
  directly.
- Run the full suite (`.venv/bin/python -m pytest tests/ -q`) before pushing.

## Reference code

The user's previous bot lives in `Brandjuh/FireAndRescueAcademyCogs`
(Red-DiscordBot cogs). When a feature says "same as the old bot", port the
logic from the matching cog there (e.g. `eventpinger`, `eventmanager`,
`buildingmanager`, `trainings_manager`).

- **Treat every reference behaviour as load-bearing until proven
  otherwise.** The old bot ran against the live game and Discord for a
  long time; oddities usually encode a real limit or incident (example:
  it archived every forum post right after writing — that guards
  Discord's 1000-active-threads-per-guild cap, which we hit only after
  re-deriving it the hard way). When dropping or changing a reference
  behaviour, either (a) name the reason it existed and why it no longer
  applies, or (b) **ask the user why it was there** before dropping it.
- **Check hard limits against growth, not just current counts** (Discord:
  1000 active threads/guild, 20 forum tags, 5 tags/post, 100-char thread
  names, 6000-char embeds; the game adds missions/members over time).
- Numbers I cannot verify from this sandbox (missionchief.com is
  unreachable here) are estimates — label them as such or ask.
