"""Build alliance buildings via Playwright browser emulation.

MissionChief's ``/buildings/new`` form is JS-driven, so we drive the real
page rather than reverse-engineering the POST. The flow mirrors a human:

1. pick the building type — its ``#detail_<id>`` block (with the buttons)
   is revealed by the page's own change handler;
2. set the name and **move the map marker**, which is what fills the
   hidden lat/lng and reverse-geocodes the read-only address (just writing
   the hidden fields leaves the address blank — the known gotcha). The
   game's reverse_address is US-centric, so for a valid worldwide pin it
   often returns nothing; when it does, we fall back to our own geocoded
   address (the build runs on the coordinates). Only a location with no
   address from *either* source is refused;
3. click the type's **"Build as Alliance Building"** button, whose jQuery
   handler sets ``build_as_alliance=1`` and submits. Coins are never
   spent: ``build_with_coins`` stays 0 and a coin-labelled button is
   refused.

Playwright is an OPTIONAL dependency. Without it this module raises
:class:`BrowserUnavailable` and the building service degrades to reporting
the resolved location for a human to build manually, keeping the bot
working on a minimal Raspberry Pi install.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

BUILDING_TYPE_IDS = {"hospital": "2", "prison": "10"}
NAME_LIMIT = 40

# 1. Select the building type — dispatches 'change', which the page's own
#    JS uses to reveal that type's #detail_<id> block (with its buttons).
_SELECT_TYPE_SCRIPT = """
(typeId) => {
    const sel = document.querySelector('#building_building_type');
    if (!sel) return {ok: false, error: 'building type select not found'};
    sel.value = String(typeId);
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: sel.value === String(typeId),
            error: sel.value === String(typeId) ? null : 'type option not available'};
}
"""

# 2. Set the name and drive the map marker to the target, exactly like a
#    real pin drop: the page fills the hidden lat/lng and reverse-geocodes
#    the (readonly) address via /reverse_address. Just writing the hidden
#    fields isn't enough — that was the address problem.
_SET_POSITION_SCRIPT = """
(cfg) => {
    const nameEl = document.querySelector('[name="building[name]"]');
    if (nameEl) nameEl.value = cfg.name;
    const coins = document.querySelector('#build_with_coins');
    if (coins) coins.value = '0';
    try {
        if (typeof building_new_marker !== 'undefined' && building_new_marker) {
            if (typeof building_new_marker.setLatLng === 'function') {        // Leaflet
                building_new_marker.setLatLng([cfg.lat, cfg.lng]);
            } else if (typeof mapkit !== 'undefined') {                       // Apple MapKit
                building_new_marker.coordinate = new mapkit.Coordinate(cfg.lat, cfg.lng);
            }
        }
        const latEl = document.querySelector('#building_latitude');
        const lngEl = document.querySelector('#building_longitude');
        if (latEl) latEl.value = cfg.lat;
        if (lngEl) lngEl.value = cfg.lng;
        if (typeof updateAddress === 'function') updateAddress();  // fills the address
        return {ok: !!(latEl && lngEl), error: (latEl && lngEl) ? null : 'lat/lng fields not found'};
    } catch (e) {
        return {ok: false, error: String(e)};
    }
}
"""

# 3a. Inspect the alliance build button for this type WITHOUT clicking, so
#     we can enforce the free-only (no-coins) guard before submitting.
_ALLIANCE_BTN_INFO_SCRIPT = """
(typeId) => {
    const detail = document.querySelector('#detail_' + typeId);
    if (!detail) return {found: false, error: 'no build detail for this type'};
    const btn = detail.querySelector('.alliance_activate');
    if (!btn) return {found: false, error: 'no "Build as Alliance Building" button'};
    return {found: true, label: (btn.value || btn.innerText || '').trim()};
}
"""

# 3b. Click it. The page's jQuery handler sets build_as_alliance=1 and the
#     button (type=submit) posts the form.
_CLICK_ALLIANCE_SCRIPT = """
(typeId) => {
    const detail = document.querySelector('#detail_' + typeId);
    const btn = detail && detail.querySelector('.alliance_activate');
    if (!btn) return {ok: false, error: 'alliance button vanished'};
    btn.click();
    return {ok: true};
}
"""


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed / usable."""


def cookies_for(base_url: str, cookie_jar) -> list[dict]:
    """Shape an aiohttp cookie jar for Playwright's add_cookies."""
    return [
        {"name": cookie.key, "value": cookie.value, "url": base_url}
        for cookie in cookie_jar
    ]


async def render_page(
    base_url: str, cookies: list[dict], path: str, *, timeout_ms: int = 30000
) -> str:
    """Return a page's HTML after JavaScript has run (authenticated).

    Used by the `!fra dump` diagnostic for JS-driven forms (e.g.
    `/buildings/new`) where the server HTML alone doesn't reflect the final
    DOM. Raises BrowserUnavailable when Playwright isn't installed.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on env
        raise BrowserUnavailable(
            "Playwright not installed; run 'pip install playwright && "
            "python -m playwright install chromium'"
        ) from exc

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            await context.add_cookies(cookies)
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)
            await page.goto(url, wait_until="networkidle")
            return await page.content()
        finally:
            await browser.close()


@dataclass
class BuildResult:
    ok: bool
    building_id: int | None
    detail: str


def _clean_name(text: str, building_type: str) -> str:
    name = " ".join((text or "").split()).strip()
    if not name:
        name = f"{building_type.capitalize()} location"
    return name[:NAME_LIMIT]


class BrowserBuilder:
    """One headless Chromium at a time (memory-safe on a Pi)."""

    def __init__(self, base_url: str, cookie_provider) -> None:
        self._base_url = base_url.rstrip("/")
        self._cookie_provider = cookie_provider  # () -> list[dict]

    @staticmethod
    def available() -> bool:
        try:
            import playwright  # noqa: F401

            return True
        except ImportError:
            return False

    async def build(
        self,
        *,
        building_type: str,
        latitude: float,
        longitude: float,
        name: str,
        address: str | None,
        dry_run: bool = False,
    ) -> BuildResult:
        """Drive the form to build the building. When ``dry_run`` is set, do
        everything except the final submit — a full check of the flow
        (type, position, address, alliance button) against the live site
        without creating anything."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on env
            raise BrowserUnavailable(
                "Playwright not installed; run 'pip install playwright && "
                "python -m playwright install chromium'"
            ) from exc

        type_id = BUILDING_TYPE_IDS.get(building_type.lower())
        if type_id is None:
            return BuildResult(False, None, f"unsupported building type {building_type!r}")

        config = {
            "typeId": type_id,
            "name": _clean_name(name, building_type),
            "lat": float(latitude),
            "lng": float(longitude),
        }
        cookies = self._cookie_provider()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],  # Pi-friendly
            )
            try:
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                await context.add_cookies(cookies)
                page = await context.new_page()
                page.set_default_timeout(30000)
                await page.goto(f"{self._base_url}/buildings/new", wait_until="networkidle")

                if await page.query_selector("input[type='password']") is not None:
                    return BuildResult(False, None, "MissionChief session is not logged in")

                # 1. Building type (reveals its #detail_<id> block).
                res = await page.evaluate(_SELECT_TYPE_SCRIPT, type_id)
                if not res.get("ok"):
                    return BuildResult(False, None, res.get("error", "could not select type"))

                # 2. Name + pin position (drives the map so the address fills).
                res = await page.evaluate(_SET_POSITION_SCRIPT, config)
                if not res.get("ok"):
                    return BuildResult(False, None, res.get("error", "could not set position"))

                # Wait for the game's own pin->address lookup (/reverse_address)
                # to land. Speed isn't a concern, so give it a fair chance.
                try:
                    await page.wait_for_function(
                        "() => { const a = document.querySelector('#building_address');"
                        " return a && a.value && a.value.trim().length > 0; }",
                        timeout=15000,
                    )
                except Exception:  # noqa: BLE001 - fallback handles an empty field
                    pass
                resolved_address = (await page.evaluate(
                    "() => { const a = document.querySelector('#building_address');"
                    " return a ? a.value : ''; }"
                ) or "").strip()

                # MissionChief's own reverse_address is US-centric and returns
                # nothing for many valid worldwide spots. When it's empty, fall
                # back to OUR geocoded address (the location is fine — the build
                # runs on the coordinates) and fill the field so it isn't blank.
                # Only refuse when there's no address from either source.
                if not resolved_address:
                    fallback = (address or "").strip()
                    if fallback:
                        await page.evaluate(
                            "(a) => { const el = document.querySelector('#building_address');"
                            " if (el) el.value = a; }",
                            fallback,
                        )
                        resolved_address = fallback
                    else:
                        return BuildResult(
                            False, None,
                            f"no address could be resolved for "
                            f"{latitude:.5f},{longitude:.5f} (neither MissionChief "
                            "nor the geocoder returned one), so nothing was built.",
                        )

                # 3. Free-only guard: the alliance button must not spend coins.
                info = await page.evaluate(_ALLIANCE_BTN_INFO_SCRIPT, type_id)
                if not info.get("found"):
                    return BuildResult(False, None, info.get("error", "no alliance build button"))
                label = info.get("label") or ""
                if "coin" in label.lower():
                    return BuildResult(False, None, "refusing to build: button would spend coins")

                if dry_run:
                    # Everything is set up correctly; stop short of submitting.
                    return BuildResult(
                        True, None,
                        f"dry-run OK — would click '{label}'; address '{resolved_address}'",
                    )

                building_id = None
                try:
                    async with page.expect_response(
                        lambda r: "/buildings" in r.url and r.request.method == "POST",
                        timeout=30000,
                    ) as resp_info:
                        clicked = await page.evaluate(_CLICK_ALLIANCE_SCRIPT, type_id)
                        if not clicked.get("ok"):
                            return BuildResult(False, None, clicked.get("error", "click failed"))
                    response = await resp_info.value
                    import re

                    match = re.search(r"/buildings/(\d+)", response.url) or re.search(
                        r"/buildings/(\d+)", str(page.url)
                    )
                    if match:
                        building_id = int(match.group(1))
                except Exception as exc:  # noqa: BLE001 - report, don't crash
                    return BuildResult(
                        False, None, f"submit did not confirm in time: {exc}"
                    )

                if building_id is None:
                    return BuildResult(
                        False, None, "built, but could not detect the new building id"
                    )
                return BuildResult(True, building_id, "created")
            finally:
                await browser.close()
