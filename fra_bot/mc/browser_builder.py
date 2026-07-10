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

# 3. Prepare the form and pick the alliance submit button — the reference
#    bot's script, adopted verbatim. Crucially it sets build_as_alliance=1
#    ITSELF as a form field and clicks a plain submit button, instead of
#    relying on the page's jQuery .alliance_activate handler (a synthetic
#    click that misses the handler would submit a PERSONAL build, which the
#    game refuses with a 200 re-render — exactly the silent failure seen
#    live). The free-only guard is the button-text filter: 'credits'
#    without 'coins'. Returns {ok, submitIndex, snapshot} or
#    {ok: false, reason, snapshot} with full field diagnostics.
_PREPARE_FORM_SCRIPT = r"""
async (config) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visibleText = (element) => [element?.value, element?.textContent, element?.getAttribute?.("title"), element?.getAttribute?.("aria-label")]
    .filter(Boolean)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  const fieldByName = (name) => document.querySelector(`[name="${window.CSS.escape(name)}"]`);
  const fieldValue = (name) => fieldByName(name)?.value || "";
  const dispatch = (field) => {
    if (!field) return;
    for (const eventName of ["input", "change"]) {
      field.dispatchEvent(new Event(eventName, { bubbles: true }));
    }
  };
  const isVisible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const setField = (name, value) => {
    const field = fieldByName(name);
    if (!field) return false;
    field.value = String(value);
    dispatch(field);
    return true;
  };
  const fail = (reason) => ({
    ok: false,
    reason,
    snapshot: {
      url: location.href,
      buildingType: fieldValue("building[building_type]"),
      name: fieldValue("building[name]"),
      latitude: fieldValue("building[latitude]"),
      longitude: fieldValue("building[longitude]"),
      address: fieldValue("building[address]"),
      buildAsAlliance: fieldValue("build_as_alliance"),
      buildWithCoins: fieldValue("build_with_coins"),
    },
  });
  function allianceContext(button) {
    for (let node = button?.parentElement; node && node !== document.body; node = node.parentElement) {
      const text = visibleText(node).toLowerCase();
      if (text.includes("build as alliance building")) return node;
      if (node.matches?.("form")) return null;
    }
    return null;
  }

  const form = document.querySelector("#new_building") || document.querySelector('form[action*="/buildings"]');
  if (!form) return fail("MissionChief building form was not loaded.");

  const typeSelect = fieldByName("building[building_type]");
  if (!typeSelect) return fail("MissionChief building type field was not found.");
  typeSelect.value = String(config.buildingTypeId || "");
  dispatch(typeSelect);
  await sleep(300);

  if (fieldValue("building[building_type]") !== String(config.buildingTypeId || "")) {
    return fail(`MissionChief did not accept building type ${config.buildingTypeId}.`);
  }
  if (!setField("building[name]", config.name || "")) return fail("MissionChief building name field was not found.");
  if (!setField("building[latitude]", config.latitude || "")) return fail("MissionChief latitude field was not found.");
  if (!setField("building[longitude]", config.longitude || "")) return fail("MissionChief longitude field was not found.");
  setField("building[address]", config.address || "");
  setField("build_with_coins", "0");
  setField("build_as_alliance", "1");
  const buildAnother = fieldByName("build_another");
  if (buildAnother) {
    buildAnother.checked = false;
    dispatch(buildAnother);
  }

  const buttons = [...document.querySelectorAll('input[type="submit"], button[type="submit"], button:not([type])')];
  const candidates = buttons
    .map((button, index) => ({ button, index, text: visibleText(button), context: allianceContext(button) }))
    .filter((item) => {
      const text = item.text.toLowerCase();
      return item.context
        && isVisible(item.button)
        && !item.button.disabled
        && !item.button.hasAttribute("disabled")
        && text.includes("build")
        && text.includes("credits")
        && !text.includes("coins");
    });

  if (candidates.length < 1) {
    return fail("No enabled alliance build button was found.");
  }
  const selected = candidates[0];
  return {
    ok: true,
    submitIndex: selected.index,
    label: selected.text,
    snapshot: {
      buildingType: fieldValue("building[building_type]"),
      name: fieldValue("building[name]"),
      latitude: fieldValue("building[latitude]"),
      longitude: fieldValue("building[longitude]"),
      address: fieldValue("building[address]"),
      buildAsAlliance: fieldValue("build_as_alliance"),
      buildWithCoins: fieldValue("build_with_coins"),
    },
  };
}
"""

# 4. Click the selected submit button (plain native click on a real submit
#    input — no dependency on page JS).
_CLICK_SUBMIT_SCRIPT = r"""
(submitIndex) => {
  const buttons = [...document.querySelectorAll('input[type="submit"], button[type="submit"], button:not([type])')];
  const button = buttons[submitIndex];
  if (!button) return false;
  button.click();
  return true;
}
"""


async def _page_flash(page) -> str:
    """The page's error flash / validation text, if any (trimmed)."""
    try:
        text = await page.evaluate(
            "() => { const el = document.querySelector("
            "'.alert-danger, .alert.alert-error, #flash_error, .flash_error,"
            " #error_explanation, .danger.alert'); "
            "return el ? el.innerText.trim() : ''; }"
        )
        return " ".join((text or "").split())[:200]
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""


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

                # The page fills #building_address through its own pin lookup
                # (/reverse_address — worldwide, same code as Leitstellenspiel),
                # but that in-page hook doesn't always fire under automation.
                # When the field stays empty, fall back to the address the
                # caller resolved (the build runs on the coordinates either
                # way) and fill the field so it isn't blank. Only refuse when
                # there's no address from either source.
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

                # 3. Fill every form field (with events) and pick the
                #    alliance submit button — the reference bot's script.
                prep = await page.evaluate(_PREPARE_FORM_SCRIPT, {
                    "buildingTypeId": type_id,
                    "name": config["name"],
                    "latitude": config["lat"],
                    "longitude": config["lng"],
                    "address": resolved_address,
                })
                if not prep.get("ok"):
                    return BuildResult(
                        False, None,
                        f"{prep.get('reason', 'form preparation failed')} "
                        f"[form: {prep.get('snapshot')}]",
                    )
                label = prep.get("label") or ""

                if dry_run:
                    # Everything is set up correctly; stop short of submitting.
                    return BuildResult(
                        True, None,
                        f"dry-run OK — would click '{label}'; "
                        f"form: {prep.get('snapshot')}",
                    )

                try:
                    async with page.expect_response(
                        lambda r: r.request.method == "POST"
                        and ("/buildings" in r.url or "/alliance_buildings" in r.url),
                        timeout=30000,
                    ) as resp_info:
                        clicked = await page.evaluate(
                            _CLICK_SUBMIT_SCRIPT, prep["submitIndex"]
                        )
                        if not clicked:
                            return BuildResult(False, None, "submit button vanished")
                    response = await resp_info.value
                except Exception as exc:  # noqa: BLE001 - report, don't crash
                    return BuildResult(
                        False, None, f"submit did not confirm in time: {exc}"
                    )

                # The old flow trusted any response here. A rejected build
                # (permissions, validation) also answers the POST — the
                # status and the page's error flash tell the difference.
                if response.status >= 400:
                    flash = await _page_flash(page)
                    return BuildResult(
                        False, None,
                        f"MissionChief rejected the build (HTTP {response.status})"
                        + (f": {flash}" if flash else ""),
                    )

                # Let the redirect (if any) land before inspecting the URL.
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001 - URL check below still runs
                    pass
                import re

                building_id = None
                match = re.search(
                    r"/(?:alliance_)?buildings/(\d+)", response.url
                ) or re.search(r"/(?:alliance_)?buildings/(\d+)", str(page.url))
                if match:
                    building_id = int(match.group(1))

                if building_id is None:
                    # Alliance builds don't redirect to /buildings/<id>, so a
                    # missing id here proves nothing either way — and a red
                    # banner on the landing page may be about something else
                    # entirely. Pass any flash text along as context and let
                    # the caller's API check deliver the verdict.
                    flash = await _page_flash(page)
                    return BuildResult(
                        True, None,
                        f"submitted (HTTP {response.status})"
                        + (f"; page shows: {flash}" if flash else "")
                        + " — needs API confirmation",
                    )
                return BuildResult(True, building_id, "created")
            finally:
                await browser.close()
