// ==UserScript==
// @name         FRA Profile Sync
// @namespace    https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot
// @version      1.1.0
// @description  Send your own MissionChief buildings and vehicles to the FRA Discord bot (profile + hotspots). NEVER sends passwords, cookies or sessions — only counts, types and building coordinates.
// @match        https://www.missionchief.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * INSTALLATION (members):
 * 1. Install Tampermonkey (Chrome/Edge) or Greasemonkey (Firefox).
 * 2. Create a new userscript and paste this file into it.
 * 3. Ask an admin for the WEBHOOK_URL and fill it in below.
 * 4. Open missionchief.com — a "Sync to FRA" button appears in the
 *    top-right corner. Click it, check the summary, confirm.
 *
 * WHAT GETS SENT? Only: your MC user id and name, your buildings
 * (counts per type + coordinates, rounded to ~100 m) and your
 * vehicles (counts per type). Nothing else. You see the summary
 * before anything is sent.
 */

(function () {
  "use strict";

  // >>> Paste the webhook URL you received from an admin here <<<
  const WEBHOOK_URL = "PASTE-THE-WEBHOOK-URL-HERE";

  const BASE = "https://www.missionchief.com";

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

  async function send(payload) {
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
    const response = await fetch(WEBHOOK_URL, { method: "POST", body: form });
    if (!response.ok) throw new Error("Webhook -> HTTP " + response.status);
  }

  async function run() {
    if (WEBHOOK_URL.indexOf("http") !== 0) {
      alert("FRA Sync: fill in the WEBHOOK_URL in the script first (ask an admin).");
      return;
    }
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
      "No passwords, cookies or sessions. Continue?";
    if (!window.confirm(summary)) return;
    try {
      await send(payload);
      alert("✅ FRA Sync complete! Your Discord profile will update shortly.");
    } catch (error) {
      alert("FRA Sync send failed: " + error.message);
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

  addButton();
})();
