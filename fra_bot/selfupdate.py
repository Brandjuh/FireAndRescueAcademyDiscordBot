"""In-place self-update: git pull + dependency install + re-exec.

Triggered from Discord (``!fra update``). The restart uses
``os.execv`` to replace the running process with a fresh interpreter,
so it works regardless of the systemd ``Restart=`` policy and needs no
sudo. The database is closed cleanly first; SQLite's WAL makes an
abrupt stop safe anyway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Repo root = parent of the fra_bot package directory.
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class UpdateResult:
    ok: bool
    changed: bool
    old_rev: str
    new_rev: str
    summary: str
    detail: str


async def _run(*args: str, cwd: Path | None = None, timeout: float = 300.0) -> tuple[int, str]:
    """Run a command, returning (exit_code, combined_output)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"command timed out after {timeout:.0f}s: {' '.join(args)}"
    return proc.returncode, (out or b"").decode("utf-8", "replace").strip()


async def _git_rev(cwd: Path) -> str:
    code, out = await _run("git", "rev-parse", "--short", "HEAD", cwd=cwd, timeout=30)
    return out if code == 0 else "unknown"


async def perform_update() -> UpdateResult:
    """Fetch and fast-forward the current branch, install deps.

    Does NOT restart — the caller triggers the re-exec after messaging.
    """
    repo = REPO_ROOT
    if not (repo / ".git").exists():
        return UpdateResult(
            False, False, "", "", "Not a git checkout",
            f"{repo} has no .git directory; can't self-update.",
        )

    old_rev = await _git_rev(repo)

    code, branch = await _run(
        "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo, timeout=30
    )
    if code != 0:
        return UpdateResult(False, False, old_rev, old_rev, "git error", branch)

    code, pull_out = await _run(
        "git", "pull", "--ff-only", "origin", branch, cwd=repo, timeout=180
    )
    if code != 0:
        return UpdateResult(
            False, False, old_rev, old_rev, "git pull failed", pull_out[:1500]
        )

    new_rev = await _git_rev(repo)
    if new_rev == old_rev:
        return UpdateResult(
            True, False, old_rev, new_rev, "Already up to date",
            f"No new commits on {branch} ({new_rev}).",
        )

    # Changelog for the update.
    _, log_out = await _run(
        "git", "log", "--oneline", f"{old_rev}..{new_rev}", cwd=repo, timeout=30
    )

    # Install any new/updated dependencies into the running venv.
    code, pip_out = await _run(
        sys.executable, "-m", "pip", "install", "-q", "-r",
        str(repo / "requirements.txt"), cwd=repo, timeout=300,
    )
    pip_note = "dependencies up to date" if code == 0 else "⚠️ pip install had issues"

    return UpdateResult(
        True,
        True,
        old_rev,
        new_rev,
        f"Updated {old_rev} → {new_rev} on {branch}",
        f"{log_out}\n\n{pip_note}" + ("" if code == 0 else f"\n{pip_out[:800]}"),
    )


def reexec() -> None:
    """Replace this process with a fresh `python -m fra_bot`.

    Never returns on success. Argv is rebuilt to re-run the module with
    the same config-path argument this process was started with.
    """
    args = [sys.executable, "-m", "fra_bot", *sys.argv[1:]]
    log.info("Re-executing for self-update: %s", " ".join(args))
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, args)
