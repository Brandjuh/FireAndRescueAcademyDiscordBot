"""Authenticated MissionChief HTTP client.

Design goals, in order:

1. **Robust** — every fetch validates that we are still logged in; on an
   expired session we re-login once and retry. Retries with exponential
   backoff on 429/5xx, honouring ``Retry-After``.
2. **Human-like** — all requests flow through a shared
   :class:`~fra_bot.core.pacing.HumanPacer` (randomized delays, hard
   per-minute cap, circuit breaker).
3. **Frugal** — cookies (including Devise's ``remember_user_token``) are
   persisted to disk, so restarts do not trigger a fresh login.

MissionChief is Ruby on Rails + Devise. Notable quirks handled here:

* A *failed* login re-renders the sign-in form with HTTP 200 (no error
  status), so success is verified with a separate authenticated GET.
* Any unauthenticated request 302-redirects to ``/users/sign_in`` —
  that redirect is our session-expiry signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from ..config import MissionChiefConfig
from ..core.pacing import HumanPacer
from .errors import FetchError, LoginError, SessionExpiredError

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SIGN_IN_PATH = "/users/sign_in"
CHECK_PATH = "/buildings"
# A response counts as logged-in when it is NOT the sign-in page and it
# either landed on a known authenticated URL or shows a logout/profile
# marker. MissionChief renders the logout link inside a dropdown that
# isn't always present in the initial HTML, so the URL is the primary
# signal (mirrors the reference cog's url_or_markers logic). Requiring a
# marker string alone falsely rejected a valid login that redirected to
# /buildings.
_AUTHENTICATED_URL_FRAGMENTS = (
    "/buildings",
    "/dashboard",
    "/missions",
    "/verband",
    "/alliance_threads",
    "/alliance_logfiles",
    "/profile",
)
_LOGGED_IN_MARKERS = ("/users/sign_out", "logout", "sign out", "my profile")

_MAX_ATTEMPTS = 3


class MissionChiefClient:
    def __init__(self, cfg: MissionChiefConfig, pacer: HumanPacer) -> None:
        self._cfg = cfg
        self._pacer = pacer
        self._session: aiohttp.ClientSession | None = None
        self._login_lock = asyncio.Lock()

    @property
    def pacer_backlog(self) -> int:
        """Requests currently waiting for their pacing turn (congestion gauge)."""
        return self._pacer.backlog

    @property
    def pacer_backlog_bulk(self) -> int:
        """How many of those are low-priority bulk requests (backfills)."""
        return self._pacer.backlog_bulk

    def reconfigure_pacing(self, mc_cfg: MissionChiefConfig) -> None:
        """Re-apply pacing settings live (after a `!fra set missionchief.*`)."""
        self._pacer.reconfigure(
            min_delay=mc_cfg.min_delay,
            max_delay=mc_cfg.max_delay,
            max_per_minute=mc_cfg.max_requests_per_minute,
            cooldown_seconds=mc_cfg.circuit_breaker_cooldown_minutes * 60.0,
        )

    # ------------------------------------------------------------------
    # Session plumbing
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=40),
                cookie_jar=aiohttp.CookieJar(),
            )
            self._load_cookies()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            self._save_cookies()
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("MissionChiefClient not started")
        return self._session

    def url(self, path: str) -> str:
        return urljoin(self._cfg.base_url + "/", path.lstrip("/"))

    # ------------------------------------------------------------------
    # Cookie persistence
    # ------------------------------------------------------------------

    def _load_cookies(self) -> None:
        path = Path(self._cfg.cookie_path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cookie = SimpleCookie()
            for item in data.get("cookies", []):
                cookie[item["name"]] = item["value"]
                if item.get("domain"):
                    cookie[item["name"]]["domain"] = item["domain"]
                if item.get("path"):
                    cookie[item["name"]]["path"] = item["path"]
            self.session.cookie_jar.update_cookies(cookie)
            log.info("Loaded %d cookies from %s", len(data.get("cookies", [])), path)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Could not load cookies from %s: %s", path, exc)

    def _save_cookies(self) -> None:
        if self._session is None:
            return
        path = Path(self._cfg.cookie_path)
        try:
            cookies = []
            for cookie in self.session.cookie_jar:
                cookies.append(
                    {
                        "name": cookie.key,
                        "value": cookie.value,
                        "domain": cookie.get("domain", ""),
                        "path": cookie.get("path", "/"),
                    }
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            # Create the temp file 0600 BEFORE writing the session token,
            # so the secret is never briefly world-readable on disk.
            payload = json.dumps({"cookies": cookies}, indent=2)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            tmp.replace(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            log.warning("Could not save cookies to %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_logged_in(final_url: str, html: str) -> bool:
        if SIGN_IN_PATH in final_url:
            return False
        if any(fragment in final_url for fragment in _AUTHENTICATED_URL_FRAGMENTS):
            return True
        lowered = html.lower()
        return any(marker in lowered for marker in _LOGGED_IN_MARKERS)

    async def login(self) -> None:
        """Perform the full Devise login flow. Raises LoginError on failure."""
        async with self._login_lock:
            await self.start()
            log.info("Logging in to MissionChief as %s", self._cfg.email)

            await self._pacer.wait_turn()
            sign_in_url = self.url(SIGN_IN_PATH)
            async with self.session.get(sign_in_url, allow_redirects=True) as resp:
                html = await resp.text()
                landed_on = str(resp.url)

            if SIGN_IN_PATH not in landed_on and self._looks_logged_in(landed_on, html):
                # Persistent cookie was still valid; nothing to do.
                log.info("Existing session still valid, skipping login POST")
                self._save_cookies()
                return

            soup = BeautifulSoup(html, "lxml")
            form = soup.find("form", method=lambda m: m and m.lower() == "post")
            if form is None:
                raise LoginError("No POST form found on sign-in page (layout change?)")
            action_url = urljoin(sign_in_url, form.get("action") or SIGN_IN_PATH)

            # Collect every input in the form: this picks up the hidden
            # authenticity_token (CSRF), utf8 and commit fields.
            fields: dict[str, str] = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    fields[name] = inp.get("value", "")
            fields["user[email]"] = self._cfg.email
            fields["user[password]"] = self._cfg.password
            fields.setdefault("user[remember_me]", "1")

            await self._pacer.wait_turn()
            async with self.session.post(
                action_url,
                data=fields,
                allow_redirects=True,
                headers={"Referer": sign_in_url},
            ) as resp:
                await resp.text()

            # Devise renders the sign-in form again with HTTP 200 on bad
            # credentials, so verify with a separate authenticated GET.
            await self._pacer.wait_turn()
            async with self.session.get(
                self.url(CHECK_PATH),
                allow_redirects=True,
                headers={"Referer": action_url},
            ) as resp:
                check_html = await resp.text()
                final_url = str(resp.url)

            if not self._looks_logged_in(final_url, check_html):
                raise LoginError(
                    "Login verification failed: check request ended on "
                    f"{final_url}. Check MC_EMAIL/MC_PASSWORD."
                )

            self._save_cookies()
            log.info("MissionChief login successful")

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    async def fetch_page(self, path: str, *, referer: str | None = None) -> str:
        """GET a MissionChief page, returning HTML.

        Handles pacing, retries and transparent re-login on session
        expiry. Raises FetchError / LoginError when all else fails.
        """
        await self.start()
        target = self.url(path)
        relogged_in = False

        for attempt in range(_MAX_ATTEMPTS):
            await self._pacer.wait_turn()
            try:
                headers = {"Referer": referer} if referer else {}
                async with self.session.get(
                    target, allow_redirects=True, headers=headers
                ) as resp:
                    status = resp.status
                    final_url = str(resp.url)
                    html = await resp.text()
                    retry_after = resp.headers.get("Retry-After")

                if status == 429 or status >= 500:
                    self._pacer.record_failure()
                    delay = _parse_retry_after(retry_after) or min(
                        5.0 * (2**attempt), 60.0
                    )
                    log.warning(
                        "HTTP %s for %s (attempt %d/%d), backing off %.1fs",
                        status, target, attempt + 1, _MAX_ATTEMPTS, delay,
                    )
                    await asyncio.sleep(delay + random.uniform(0.0, 1.0))
                    continue

                if status >= 400:
                    self._pacer.record_failure()
                    raise FetchError(target, status)

                if SIGN_IN_PATH in final_url:
                    # Session expired mid-scrape: re-login once, then retry.
                    if relogged_in:
                        raise SessionExpiredError(
                            f"Still redirected to sign-in after re-login ({target})"
                        )
                    log.info("Session expired (redirect to sign-in); re-logging in")
                    relogged_in = True
                    await self.login()
                    continue

                self._pacer.record_success()
                return html

            except aiohttp.ClientError as exc:
                self._pacer.record_failure()
                delay = min(5.0 * (2**attempt), 60.0)
                log.warning(
                    "Network error fetching %s (attempt %d/%d): %s — retrying in %.1fs",
                    target, attempt + 1, _MAX_ATTEMPTS, exc, delay,
                )
                await asyncio.sleep(delay + random.uniform(0.0, 1.0))

        raise FetchError(target, message=f"Gave up on {target} after {_MAX_ATTEMPTS} attempts")

    async def post_form(
        self,
        path: str,
        data: dict[str, str] | list[tuple[str, str]],
        *,
        referer: str | None = None,
        ajax: bool = False,
        csrf_token: str | None = None,
        allow_redirects: bool = True,
    ) -> tuple[int, str, str]:
        """POST a Rails form. Returns (status, html, final_url).

        The CSRF ``authenticity_token`` must already be in ``data`` (it
        comes from the page's form); ``ajax=True`` adds the XHR headers
        MissionChief expects for its JS endpoints, with the token also
        in the ``X-CSRF-Token`` header.

        One attempt only, no transparent re-login: POSTs are actions,
        and blindly repeating an action that may have landed is worse
        than failing loudly. Callers decide whether to retry.
        """
        await self.start()
        target = self.url(path)
        await self._pacer.wait_turn()

        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers.update(
                {
                    "Accept": (
                        "text/javascript, application/javascript, "
                        "application/ecmascript, */*; q=0.01"
                    ),
                    "Origin": self._cfg.base_url,
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
            if csrf_token:
                headers["X-CSRF-Token"] = csrf_token

        try:
            async with self.session.post(
                target, data=data, headers=headers, allow_redirects=allow_redirects
            ) as resp:
                status = resp.status
                final_url = str(resp.url)
                location = resp.headers.get("Location", "")
                html = await resp.text()
        except aiohttp.ClientError as exc:
            self._pacer.record_failure()
            raise FetchError(target, message=f"POST to {target} failed: {exc}") from exc

        # Session expiry shows up either as a followed redirect ending on
        # the sign-in page, or (with allow_redirects=False) as a 3xx whose
        # Location points there. Both must be caught so a Devise
        # re-render is never scored as a successful action.
        if SIGN_IN_PATH in final_url or SIGN_IN_PATH in location:
            raise SessionExpiredError(f"POST to {target} redirected to sign-in")
        if status >= 400:
            self._pacer.record_failure()
        else:
            self._pacer.record_success()
        return status, html, final_url

    async def verify_session(self) -> bool:
        """Cheap health check: are we still logged in?"""
        try:
            await self.fetch_page(CHECK_PATH)
            return True
        except (FetchError, LoginError, SessionExpiredError):
            return False


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None
