"""Build alliance buildings via Playwright browser emulation.

MissionChief's ``/buildings/new`` form is JS-driven (the building-type
<select> fires scripts, the map marker is set programmatically, and the
correct "Build as alliance building (credits)" submit button is chosen
by live DOM context). Reverse-engineering the exact Rails POST is
fragile, so we drive the real form — exactly what the reference cog did.

Playwright is an OPTIONAL dependency. If it isn't installed, this module
raises :class:`BrowserUnavailable` and the building service degrades to
reporting the resolved location for a human to build manually. That
keeps the bot working on a minimal Raspberry Pi install.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

BUILDING_TYPE_IDS = {"hospital": "2", "prison": "10"}
NAME_LIMIT = 40

# Fill the form but do NOT submit; return the index of the alliance
# credits submit button.
_PREPARE_SCRIPT = """
(config) => {
    const form = document.querySelector('#new_building')
        || document.querySelector('form[action*="/buildings"]');
    if (!form) return {ok: false, error: 'building form not found'};

    const setSelect = (name, value) => {
        const el = form.querySelector(`[name="${name}"]`);
        if (!el) return false;
        el.value = value;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return el.value === value;
    };
    const setValue = (name, value) => {
        const el = form.querySelector(`[name="${name}"]`);
        if (el) { el.value = value; el.dispatchEvent(new Event('input', {bubbles: true})); }
    };

    if (!setSelect('building[building_type]', config.typeId))
        return {ok: false, error: 'could not set building type'};
    setValue('building[name]', config.name);
    setValue('building[latitude]', config.lat);
    setValue('building[longitude]', config.lng);
    setValue('building[address]', config.address || '');
    const coins = form.querySelector('[name="build_with_coins"]');
    if (coins) coins.value = '0';
    const asAlliance = form.querySelector('[name="build_as_alliance"]');
    if (asAlliance) asAlliance.value = '1';

    const buttons = Array.from(form.querySelectorAll('button, input[type=submit]'));
    let index = -1;
    buttons.forEach((btn, i) => {
        const text = (btn.innerText || btn.value || '').toLowerCase();
        const context = (btn.closest('div, form') || {}).innerText || '';
        const allianceCtx = context.toLowerCase().includes('alliance');
        const visible = btn.offsetParent !== null && !btn.disabled;
        if (index === -1 && visible && text.includes('build')
            && text.includes('credit') && !text.includes('coin')) {
            index = i;
        }
    });
    return {ok: index >= 0, index, error: index >= 0 ? null : 'alliance submit button not found'};
}
"""

_CLICK_SCRIPT = """
(index) => {
    const form = document.querySelector('#new_building')
        || document.querySelector('form[action*="/buildings"]');
    const buttons = Array.from(form.querySelectorAll('button, input[type=submit]'));
    buttons[index].click();
    return true;
}
"""


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed / usable."""


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
    ) -> BuildResult:
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
            "lat": f"{latitude:.7f}",
            "lng": f"{longitude:.7f}",
            "address": address or "",
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

                prepared = await page.evaluate(_PREPARE_SCRIPT, config)
                if not prepared.get("ok"):
                    return BuildResult(False, None, prepared.get("error", "form prep failed"))

                building_id = None
                try:
                    async with page.expect_response(
                        lambda r: "/buildings" in r.url and r.request.method == "POST",
                        timeout=30000,
                    ) as info:
                        await page.evaluate(_CLICK_SCRIPT, prepared["index"])
                    response = await info.value
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
