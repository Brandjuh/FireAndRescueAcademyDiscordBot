// ==UserScript==
// @name         FRA Profile Sync
// @namespace    https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot
// @version      1.0.0
// @description  Stuur je eigen MissionChief-gebouwen en -voertuigen naar de FRA Discord-bot (profiel + hotspots). Verstuurt NOOIT wachtwoorden, cookies of sessies — alleen aantallen, types en gebouwcoördinaten.
// @match        https://www.missionchief.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * INSTALLATIE (leden):
 * 1. Installeer Tampermonkey (Chrome/Edge) of Greasemonkey (Firefox).
 * 2. Maak een nieuw userscript aan en plak dit bestand erin.
 * 3. Vraag de WEBHOOK_URL aan een admin en vul hem hieronder in.
 * 4. Open missionchief.com — rechtsboven verschijnt een knop
 *    "Sync naar FRA". Klik, controleer de samenvatting, bevestig.
 *
 * WAT WORDT ER VERSTUURD? Alleen: je MC-gebruikers-id en -naam, je
 * gebouwen (aantallen per type + coördinaten, afgerond op ~100 m) en
 * je voertuigen (aantallen per type). Niets anders. Je ziet de
 * samenvatting vóór het versturen.
 */

(function () {
  "use strict";

  // >>> Vul hier de webhook-URL in die je van een admin kreeg <<<
  const WEBHOOK_URL = "PLAK-HIER-DE-WEBHOOK-URL";

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
    if (!mcUserId) throw new Error("Kon je MC-gebruikers-id niet vinden — open je dashboard en probeer opnieuw.");
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
      alert("FRA Sync: vul eerst de WEBHOOK_URL in het script in (vraag een admin).");
      return;
    }
    let payload;
    try {
      payload = await buildPayload();
    } catch (error) {
      alert("FRA Sync mislukt: " + error.message);
      return;
    }
    const summary =
      "Dit wordt naar de FRA-bot gestuurd:\n\n" +
      "MC-account: " + (payload.mc_name || "?") + " (" + payload.mc_user_id + ")\n" +
      "Gebouwen: " + payload.buildings.total + " (met locaties)\n" +
      "Voertuigen: " + payload.vehicles.total + "\n\n" +
      "Geen wachtwoorden, cookies of sessies. Doorgaan?";
    if (!window.confirm(summary)) return;
    try {
      await send(payload);
      alert("✅ FRA Sync gelukt! Je profiel in Discord wordt zo bijgewerkt.");
    } catch (error) {
      alert("FRA Sync versturen mislukt: " + error.message);
    }
  }

  function addButton() {
    if (document.getElementById("fra-sync-button")) return;
    const nav = document.querySelector(".navbar .nav, .navbar-right, #navbar-main-collapse");
    const button = document.createElement("a");
    button.id = "fra-sync-button";
    button.textContent = "🔄 Sync naar FRA";
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
