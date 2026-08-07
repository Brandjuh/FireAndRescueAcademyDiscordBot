// ==UserScript==
// @name         FRA Profile Sync
// @namespace    https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot
// @version      2.0.0
// @description  Send your own MissionChief buildings and vehicles to the FRA Discord bot (profile + hotspots). NEVER sends passwords, cookies or sessions — only counts, types and building coordinates.
// @match        https://www.missionchief.com/*
// @grant        none
// @run-at       document-idle
// @updateURL    https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/tools/fra-profile-sync.user.js
// @downloadURL  https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/tools/fra-profile-sync.user.js
// ==/UserScript==

/*
 * INSTALLATION (members):
 * 1. Install Tampermonkey (Chrome/Edge) or Greasemonkey (Firefox).
 * 2. Open the install link from the FRA Discord panel (the 📥 button) —
 *    Tampermonkey picks the script up automatically. Updates install
 *    themselves from the same link.
 * 3. Open the missionchief.com MAIN page (the dashboard with the map)
 *    — a "🔄 Sync to FRA" button appears in the navbar. Click it.
 * 4. The script asks ONCE for your sync link: get it with the
 *    "🔑 Get sync link" button on the FRA Discord panel and paste it.
 *    Check the summary, confirm. Done.
 *
 * WHAT DOES IT DO? On a sync it reads YOUR OWN /api/buildings and
 * /api/vehicles with your own browser session and sends only: your MC
 * user id and name, building counts per type + coordinates rounded to
 * ~100 m, and vehicle counts per type. Nothing else — no passwords,
 * cookies, sessions, credits, chat, or anyone else's data. You see the
 * exact summary before your first send.
 *
 * WHEN? On your click, and automatically about once a day while you
 * have MissionChief open (silent, same data). Remove the script from
 * Tampermonkey to stop syncing; delete your stored data any time with
 * the 🗑️ button on the FRA Discord panel.
 */

(function () {
  "use strict";

  // Manual override for the sync link — normally leave this alone: the
  // script asks for the link once and remembers it. (Editing this line
  // does NOT survive script auto-updates; the remembered link does.)
  const WEBHOOK_URL = "PASTE-THE-WEBHOOK-URL-HERE";

  const BASE = "https://www.missionchief.com";
  const STORAGE_KEY = "fra_sync_webhook_url";
  const LAST_SYNC_KEY = "fra_sync_last_auto";
  const AUTO_SYNC_HOURS = 24;
  const WEBHOOK_RE =
    /^https:\/\/(?:ptb\.|canary\.)?discord(?:app)?\.com\/api\/webhooks\/\d+\/[\w-]+$/;

  // localStorage can be unavailable (private mode) — fall back to a
  // per-page-load session variable so a manual sync still works.
  let sessionUrl = null;

  function storageGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return key === STORAGE_KEY ? sessionUrl : null;
    }
  }

  function storageSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      if (key === STORAGE_KEY) sessionUrl = value;
    }
  }

  function storageDel(key) {
    try {
      window.localStorage.removeItem(key);
    } catch (error) {
      if (key === STORAGE_KEY) sessionUrl = null;
    }
  }

  function getWebhookUrl(interactive) {
    if (WEBHOOK_RE.test(WEBHOOK_URL)) {
      storageSet(STORAGE_KEY, WEBHOOK_URL); // hand-edit survives updates
      return WEBHOOK_URL;
    }
    const stored = storageGet(STORAGE_KEY);
    if (stored && WEBHOOK_RE.test(stored)) return stored;
    if (!interactive) return null; // auto-sync never prompts
    const entered = window.prompt(
      "Paste your FRA sync link (get it with the '🔑 Get sync link' " +
      "button on the FRA Discord panel):"
    );
    if (!entered) return null; // cancelled
    const trimmed = entered.trim();
    if (!WEBHOOK_RE.test(trimmed)) {
      alert("That doesn't look like a sync link — get a fresh one with " +
            "the 🔑 button in the FRA Discord.");
      return null;
    }
    storageSet(STORAGE_KEY, trimmed);
    return trimmed;
  }

  function findUserId() {
    // The game exposes the logged-in user id as a global on most pages;
    // fall back to the navbar profile link. (Both are estimates against
    // live markup — adjust here if the game changes.)
    if (typeof window.user_id !== "undefined" && window.user_id) {
      return parseInt(window.user_id, 10);
    }
    const link = document.querySelector('a[href^="/profile/"]');
    if (link) {
      const match = link.getAttribute("href").match(/\/profile\/(\d+)/);
      if (match) return parseInt(match[1], 10);
    }
    return null;
  }

  function findUserName() {
    const link = document.querySelector('a[href^="/profile/"]');
    return link ? link.textContent.trim() : null;
  }

  async function fetchJson(path) {
    const response = await fetch(BASE + path, { credentials: "same-origin" });
    if (!response.ok) throw new Error(path + " -> HTTP " + response.status);
    return response.json();
  }

  function countByType(rows, typeKeys) {
    const byType = {};
    for (const row of rows) {
      let typeId = null;
      for (const key of typeKeys) {
        if (row[key] !== undefined && row[key] !== null) {
          typeId = row[key];
          break;
        }
      }
      const bucket = String(parseInt(typeId, 10) >= 0 ? parseInt(typeId, 10) : -1);
      byType[bucket] = (byType[bucket] || 0) + 1;
    }
    return byType;
  }

  async function buildPayload() {
    const mcUserId = findUserId();
    if (!mcUserId) throw new Error("Could not find your MC user id — open your dashboard and try again.");
    const [buildings, vehicles] = await Promise.all([
      fetchJson("/api/buildings"),
      fetchJson("/api/vehicles"),
    ]);
    const coords = [];
    for (const building of buildings) {
      const lat = parseFloat(building.latitude ?? building.lat);
      const lng = parseFloat(building.longitude ?? building.lon ?? building.lng);
      if (isFinite(lat) && isFinite(lng)) {
        coords.push([Math.round(lat * 1000) / 1000, Math.round(lng * 1000) / 1000]);
      }
    }
    return {
      fra_profile_sync: 1,
      mc_user_id: mcUserId,
      mc_name: findUserName(),
      synced_at: new Date().toISOString(),
      buildings: {
        total: buildings.length,
        by_type: countByType(buildings, ["building_type", "building_type_id"]),
        coords: coords,
      },
      vehicles: {
        total: vehicles.length,
        by_type: countByType(vehicles, ["vehicle_type", "vehicle_type_id"]),
      },
    };
  }

  async function send(payload, url) {
    // JSON as a FILE attachment: webhook message content caps at 2000
    // chars; a fleet's coordinate list does not fit inline.
    const form = new FormData();
    form.append("payload_json", JSON.stringify({
      content: "FRA profile sync: " + (payload.mc_name || payload.mc_user_id),
    }));
    form.append(
      "files[0]",
      new Blob([JSON.stringify(payload)], { type: "application/json" }),
      "fra-profile-sync.json"
    );
    const response = await fetch(url, { method: "POST", body: form });
    if (response.status === 401 || response.status === 403 || response.status === 404) {
      // The link was rotated or deleted server-side: purge it so the
      // next manual sync asks for a fresh one instead of failing forever.
      storageDel(STORAGE_KEY);
      throw new Error(
        "your sync link is no longer valid — click '🔑 Get sync link' " +
        "in the FRA Discord and sync again."
      );
    }
    if (!response.ok) throw new Error("Webhook -> HTTP " + response.status);
  }

  function markSynced() {
    storageSet(LAST_SYNC_KEY, String(Date.now()));
  }

  async function run() {
    const url = getWebhookUrl(true);
    if (!url) return;
    let payload;
    try {
      payload = await buildPayload();
    } catch (error) {
      alert("FRA Sync failed: " + error.message);
      return;
    }
    const summary =
      "This will be sent to the FRA bot:\n\n" +
      "MC account: " + (payload.mc_name || "?") + " (" + payload.mc_user_id + ")\n" +
      "Buildings: " + payload.buildings.total + " (with locations)\n" +
      "Vehicles: " + payload.vehicles.total + "\n\n" +
      "No passwords, cookies or sessions. The script will refresh this\n" +
      "automatically about once a day while you play. Continue?";
    if (!window.confirm(summary)) return;
    try {
      markSynced(); // before the POST — a second tab must not double-send
      await send(payload, url);
      alert("✅ FRA Sync complete! Your Discord profile will update shortly. " +
            "From now on the script refreshes automatically about once a day.");
    } catch (error) {
      alert("FRA Sync send failed: " + error.message);
    }
  }

  async function autoSync() {
    // Silent daily refresh. Runs only when a sync link is already stored
    // (the FIRST sync is always manual — that's where the member sees and
    // confirms exactly what is sent). Never prompts, never alerts: this
    // fires on ordinary page loads.
    const url = getWebhookUrl(false);
    if (!url) return;
    const last = parseInt(storageGet(LAST_SYNC_KEY) || "0", 10);
    if (Date.now() - last < AUTO_SYNC_HOURS * 3600 * 1000) return;
    markSynced(); // before the POST — parallel tabs must not double-send
    try {
      const payload = await buildPayload();
      await send(payload, url);
      console.info("[FRA Sync] auto-synced", payload.buildings.total,
                   "buildings /", payload.vehicles.total, "vehicles");
    } catch (error) {
      // A rotated link was already purged by send(); anything else waits
      // for the next window. No alert — this must never nag on page load.
      console.warn("[FRA Sync] auto-sync failed:", error.message);
    }
  }

  function addButton() {
    if (document.getElementById("fra-sync-button")) return;
    const nav = document.querySelector(".navbar .nav, .navbar-right, #navbar-main-collapse");
    const button = document.createElement("a");
    button.id = "fra-sync-button";
    button.textContent = "🔄 Sync to FRA";
    button.href = "#";
    button.style.cssText =
      "display:inline-block;padding:6px 10px;margin:6px;background:#c0392b;" +
      "color:#fff;border-radius:4px;font-weight:bold;text-decoration:none;";
    button.addEventListener("click", function (event) {
      event.preventDefault();
      run();
    });
    if (nav) {
      nav.appendChild(button);
    } else {
      button.style.cssText += "position:fixed;top:8px;right:8px;z-index:9999;";
      document.body.appendChild(button);
    }
  }

  // The manual button only on the game's main page (a sync button on
  // every subpage is noise); the silent daily refresh runs from any
  // page — the APIs are reachable everywhere.
  if (window.location.pathname === "/") {
    addButton();
  }
  autoSync();
})();
