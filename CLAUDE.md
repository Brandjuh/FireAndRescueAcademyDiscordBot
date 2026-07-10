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
