// ==UserScript==
// @name         FRA Auto-Build (private)
// @namespace    https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot
// @version      1.7.0
// @description  Private admin tool: bulk-build YOUR OWN MissionChief buildings. Pick a type, pick a place (fixed area and/or random worldwide), flip the toggle. Every new building is linked to the nearest dispatch center, fully expanded (all extensions/storage bought with credits) and fire stations start with a Quint.
// @match        https://www.missionchief.com/*
// @match        https://missionchief.com/*
// @grant        none
// @run-at       document-idle
// @updateURL    https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/tools/fra-auto-build.user.js
// @downloadURL  https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/tools/fra-auto-build.user.js
// ==/UserScript==

/*
 * WHAT THIS IS
 * ------------
 * This is NOT the member profile-sync script (tools/fra-profile-sync.user.js)
 * and it sends nothing anywhere: it only clicks around missionchief.com in
 * your own logged-in browser, on your own account. It is meant for one
 * person (the alliance admin) who wants to spend a large personal credit
 * balance on buildings without doing 300 identical form fills by hand.
 *
 * WHAT IT DOES, PER BUILDING
 * --------------------------
 * 0. takes the next building type from the ones you ticked — the list is
 *    the game's own, read from the build form, and you can tick all of
 *    them; they are then built in turn, evenly;
 * 1. picks a spot — random inside a radius around a place you name, random
 *    anywhere in the world, or alternating between the two;
 * 2. checks the spot has a real street address (the game's own pin lookup,
 *    with OpenStreetMap as a second opinion) so nothing lands in the sea.
 *    When OSM knows a real station/hospital/prison of that kind nearby,
 *    the spot MOVES onto it and the building takes ITS OSM name — no
 *    invented names, and never a city name from tens of kilometres away;
 * 3. reads the game's build form (/buildings/new — since late 2026 a bare
 *    HTML fragment that only works inside the game's own map page, so it
 *    is NOT opened in a frame any more), fills in type, name, pin, address
 *    (the game's own /reverse_address lookup), the dispatch center and —
 *    for fire stations — the Quint as the free starting vehicle;
 * 4. posts it exactly the way the game's own form does, with credits.
 *    NEVER with coins: build_with_coins is 0, and the personal
 *    build_credits_<type> button must exist, be enabled and say Credits;
 * 5. saves the staff amount (building[personal_count_target]) and, if the
 *    build did not take it, the dispatch center that covers that REGION —
 *    the center matched by name against country/region/city, with your
 *    own rule list on top ("Netherlands = Rotterdam Dispatch").
 *    A country where you have no center is never built in: the script
 *    skips it and puts the country on a list in the panel to ask you about;
 * 6. raises the LEVEL as far as it goes (/buildings/<id>/expand) and buys
 *    every STORAGE slot. Extensions are OFF by default — switch them on
 *    yourself if you want them;
 * 7. keeps the building on a "finish" list, because storage rooms and
 *    extensions only unlock when the previous one finishes CONSTRUCTION
 *    (days) — the list is revisited until nothing is left to buy.
 *
 * Every step is a plain same-origin request with your session and the
 * page's CSRF token — no hidden frame, no clicking.
 *
 * SAFETY RAILS
 * ------------
 * - Coins are never spent (see 4).
 * - A credit floor stops the run before your balance is gone.
 * - A per-session cap and a minimum interval between builds.
 * - A duplicate guard: never build within 250 m of something this script
 *   already built.
 * - DRY RUN is on by default: it does everything except the final click,
 *   and tells you exactly what it would have clicked — including every
 *   purchase it would make on the new building. Read that list once before
 *   you switch it off.
 * - Every build is a PERSONAL build, with your own credits. There is no
 *   alliance mode.
 * - Only one browser tab ever runs the loop (the others see "another tab is
 *   driving").
 *
 * FIRST RUN
 * ---------
 * Install in Tampermonkey, open missionchief.com, open the panel via the
 * "FRA" menu in the navbar (🏗️ Auto-Build), press "Self-test". That reads
 * the real build form and reports what it found: every building type with
 * its personal credits button, the starting vehicle, the game's address
 * lookup, your dispatch centers, and the level/storage/staff controls on
 * one of your fire stations. Then pick a type, set a location, leave DRY
 * RUN on, and press Start once.
 */

(function () {
  "use strict";

  if (window.top !== window.self) return;   // never inside our own frames

  const VERSION = "1.7.0";
  // Both www.missionchief.com and missionchief.com serve the game, and a
  // frame or fetch across those two is CROSS-ORIGIN: everything here would
  // silently stop working. So the script always stays on the host the
  // player is actually on.
  const BASE = /missionchief\.com$/i.test(window.location.hostname)
    ? window.location.origin
    : "https://www.missionchief.com";
  const SETTINGS_KEY = "fra_autobuild_settings";
  const QUEUE_KEY = "fra_autobuild_queue";
  const HISTORY_KEY = "fra_autobuild_history";
  const TYPES_KEY = "fra_autobuild_types";
  const SESSION_KEY = "fra_autobuild_session";
  const NEEDS_KEY = "fra_autobuild_needs_dispatch";
  const OWNER_KEY = "fra_autobuild_owner";
  const PANEL_OPEN_KEY = "fra_autobuild_panel_open";

  // One tab drives; the others idle. Same idea as the bot's job lock: a
  // heartbeat that goes stale when a tab is closed mid-run.
  const TAB_ID = Math.random().toString(36).slice(2);
  const OWNER_HEARTBEAT_MS = 5000;
  const OWNER_STALE_MS = 20000;

  const MAX_LOCATION_TRIES = 12;     // per build: re-jitter, then re-pick
  const FINISH_IDLE_LIMIT = 3;       // quiet visits before a building retires
  const EDIT_MAX_TRIES = 5;          // then stop retrying dispatch/staff
  const FINISH_INTERVAL_MS = 5 * 60 * 1000;
  // Storage rooms and extensions take DAYS to build, and the next one stays
  // greyed out until then. A building with such a purchase still waiting is
  // checked this often, for at most this long.
  const FINISH_WAIT_INTERVAL_MS = 60 * 60 * 1000;
  const FINISH_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
  const DUPLICATE_RADIUS_M = 250;    // same figure the bot uses
  const NOMINATIM_REVERSE =
    "https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18";
  const OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
  ];
  const OVERPASS_TIMEOUT_MS = 12000;

  const DEFAULTS = {
    enabled: false,          // the toggle survives a page load
    dryRun: true,
    typeValues: [],                  // every type you ticked; built in turn
    locationMode: "world",           // "area" | "world" | "mix"
    areaText: "",
    areaLat: null,
    areaLng: null,
    areaRadiusKm: 25,
    jitterKm: 12,
    intervalSeconds: 90,
    maxPerSession: 10,
    creditsFloor: 5000000,
    verifyWithOsm: true,
    useOsmNames: true,               // name (and place) buildings from OSM
    facilityRadiusKm: 3,
    linkDispatch: true,
    dispatchRules: "",               // "Netherlands = Dispatch Rotterdam" per line
    maxLevel: true,                  // raise the level as far as it goes
    setStaffLimit: true,
    staffLimit: 400,
    buyStorage: true,
    maxStorageBuys: 12,
    buyExtensions: false,            // OFF on purpose — see the header
    startingVehicle: "Quint",
    strictVehicle: true,
  };

  // Approximate city centres (to ~0.1°, good enough — every point is
  // jittered by kilometres anyway and then address-checked). Purely a
  // spread of inhabited places, mirroring the bot's WORLD_CITIES pool.
  const WORLD_POINTS = [
    [40.71, -74.01, "New York, USA"], [34.05, -118.24, "Los Angeles, USA"],
    [41.88, -87.63, "Chicago, USA"], [29.76, -95.37, "Houston, USA"],
    [33.45, -112.07, "Phoenix, USA"], [39.95, -75.17, "Philadelphia, USA"],
    [32.78, -96.80, "Dallas, USA"], [25.77, -80.19, "Miami, USA"],
    [33.75, -84.39, "Atlanta, USA"], [39.74, -104.99, "Denver, USA"],
    [47.61, -122.33, "Seattle, USA"], [42.36, -71.06, "Boston, USA"],
    [42.33, -83.05, "Detroit, USA"], [44.98, -93.27, "Minneapolis, USA"],
    [38.63, -90.20, "St. Louis, USA"], [36.17, -115.14, "Las Vegas, USA"],
    [43.65, -79.38, "Toronto, Canada"], [49.28, -123.12, "Vancouver, Canada"],
    [45.50, -73.57, "Montreal, Canada"], [51.05, -114.07, "Calgary, Canada"],
    [45.42, -75.70, "Ottawa, Canada"], [19.43, -99.13, "Mexico City, Mexico"],
    [20.67, -103.35, "Guadalajara, Mexico"], [25.69, -100.32, "Monterrey, Mexico"],
    [23.11, -82.37, "Havana, Cuba"], [8.98, -79.52, "Panama City, Panama"],
    [9.93, -84.08, "San Jose, Costa Rica"], [17.97, -76.79, "Kingston, Jamaica"],
    [-23.55, -46.63, "Sao Paulo, Brazil"], [-22.91, -43.17, "Rio de Janeiro, Brazil"],
    [-15.79, -47.88, "Brasilia, Brazil"], [-30.03, -51.23, "Porto Alegre, Brazil"],
    [-34.60, -58.38, "Buenos Aires, Argentina"], [-31.42, -64.18, "Cordoba, Argentina"],
    [-33.45, -70.67, "Santiago, Chile"], [-12.05, -77.04, "Lima, Peru"],
    [4.71, -74.07, "Bogota, Colombia"], [6.24, -75.57, "Medellin, Colombia"],
    [10.49, -66.88, "Caracas, Venezuela"], [-0.18, -78.47, "Quito, Ecuador"],
    [-34.90, -56.16, "Montevideo, Uruguay"], [-16.50, -68.15, "La Paz, Bolivia"],
    [-25.28, -57.64, "Asuncion, Paraguay"],
    [51.51, -0.13, "London, United Kingdom"], [53.48, -2.24, "Manchester, United Kingdom"],
    [52.48, -1.90, "Birmingham, United Kingdom"], [55.86, -4.25, "Glasgow, United Kingdom"],
    [53.35, -6.26, "Dublin, Ireland"], [48.86, 2.35, "Paris, France"],
    [43.30, 5.37, "Marseille, France"], [45.76, 4.84, "Lyon, France"],
    [40.42, -3.70, "Madrid, Spain"], [41.39, 2.17, "Barcelona, Spain"],
    [39.47, -0.38, "Valencia, Spain"], [38.72, -9.14, "Lisbon, Portugal"],
    [41.15, -8.61, "Porto, Portugal"], [52.37, 4.90, "Amsterdam, Netherlands"],
    [51.92, 4.48, "Rotterdam, Netherlands"], [52.09, 5.12, "Utrecht, Netherlands"],
    [50.85, 4.35, "Brussels, Belgium"], [51.22, 4.40, "Antwerp, Belgium"],
    [52.52, 13.40, "Berlin, Germany"], [48.14, 11.58, "Munich, Germany"],
    [53.55, 9.99, "Hamburg, Germany"], [50.94, 6.96, "Cologne, Germany"],
    [50.11, 8.68, "Frankfurt, Germany"], [47.37, 8.54, "Zurich, Switzerland"],
    [48.21, 16.37, "Vienna, Austria"], [41.90, 12.50, "Rome, Italy"],
    [45.46, 9.19, "Milan, Italy"], [40.85, 14.27, "Naples, Italy"],
    [45.07, 7.69, "Turin, Italy"], [55.68, 12.57, "Copenhagen, Denmark"],
    [59.33, 18.07, "Stockholm, Sweden"], [59.91, 10.75, "Oslo, Norway"],
    [60.17, 24.94, "Helsinki, Finland"], [52.23, 21.01, "Warsaw, Poland"],
    [50.06, 19.94, "Krakow, Poland"], [50.08, 14.44, "Prague, Czechia"],
    [47.50, 19.04, "Budapest, Hungary"], [44.43, 26.10, "Bucharest, Romania"],
    [37.98, 23.73, "Athens, Greece"], [50.45, 30.52, "Kyiv, Ukraine"],
    [44.79, 20.45, "Belgrade, Serbia"], [45.81, 15.98, "Zagreb, Croatia"],
    [42.70, 23.32, "Sofia, Bulgaria"],
    [30.04, 31.24, "Cairo, Egypt"], [31.20, 29.92, "Alexandria, Egypt"],
    [6.52, 3.38, "Lagos, Nigeria"], [9.06, 7.49, "Abuja, Nigeria"],
    [-1.29, 36.82, "Nairobi, Kenya"], [-4.04, 39.67, "Mombasa, Kenya"],
    [5.60, -0.19, "Accra, Ghana"], [14.72, -17.47, "Dakar, Senegal"],
    [33.57, -7.59, "Casablanca, Morocco"], [31.63, -8.01, "Marrakesh, Morocco"],
    [36.81, 10.18, "Tunis, Tunisia"], [36.75, 3.06, "Algiers, Algeria"],
    [9.03, 38.74, "Addis Ababa, Ethiopia"], [0.35, 32.58, "Kampala, Uganda"],
    [-6.79, 39.21, "Dar es Salaam, Tanzania"], [-33.92, 18.42, "Cape Town, South Africa"],
    [-26.20, 28.05, "Johannesburg, South Africa"], [-29.86, 31.02, "Durban, South Africa"],
    [-8.84, 13.23, "Luanda, Angola"], [-4.32, 15.31, "Kinshasa, DR Congo"],
    [41.01, 28.98, "Istanbul, Turkey"], [39.93, 32.86, "Ankara, Turkey"],
    [32.08, 34.78, "Tel Aviv, Israel"], [25.20, 55.27, "Dubai, UAE"],
    [24.45, 54.38, "Abu Dhabi, UAE"], [25.29, 51.53, "Doha, Qatar"],
    [24.71, 46.68, "Riyadh, Saudi Arabia"], [21.49, 39.19, "Jeddah, Saudi Arabia"],
    [31.95, 35.93, "Amman, Jordan"], [33.89, 35.50, "Beirut, Lebanon"],
    [29.38, 47.99, "Kuwait City, Kuwait"],
    [35.68, 139.69, "Tokyo, Japan"], [34.69, 135.50, "Osaka, Japan"],
    [37.57, 126.98, "Seoul, South Korea"], [35.18, 129.08, "Busan, South Korea"],
    [39.90, 116.41, "Beijing, China"], [31.23, 121.47, "Shanghai, China"],
    [23.13, 113.26, "Guangzhou, China"], [22.32, 114.17, "Hong Kong"],
    [25.03, 121.57, "Taipei, Taiwan"], [28.61, 77.21, "Delhi, India"],
    [19.08, 72.88, "Mumbai, India"], [12.97, 77.59, "Bengaluru, India"],
    [13.08, 80.27, "Chennai, India"], [22.57, 88.36, "Kolkata, India"],
    [24.86, 67.01, "Karachi, Pakistan"], [31.55, 74.34, "Lahore, Pakistan"],
    [23.81, 90.41, "Dhaka, Bangladesh"], [13.76, 100.50, "Bangkok, Thailand"],
    [21.03, 105.85, "Hanoi, Vietnam"], [10.82, 106.63, "Ho Chi Minh City, Vietnam"],
    [-6.21, 106.85, "Jakarta, Indonesia"], [-7.25, 112.75, "Surabaya, Indonesia"],
    [3.14, 101.69, "Kuala Lumpur, Malaysia"], [1.35, 103.82, "Singapore"],
    [14.60, 120.98, "Manila, Philippines"], [10.32, 123.89, "Cebu City, Philippines"],
    [43.24, 76.89, "Almaty, Kazakhstan"], [41.30, 69.24, "Tashkent, Uzbekistan"],
    [-33.87, 151.21, "Sydney, Australia"], [-37.81, 144.96, "Melbourne, Australia"],
    [-27.47, 153.03, "Brisbane, Australia"], [-31.95, 115.86, "Perth, Australia"],
    [-34.93, 138.60, "Adelaide, Australia"], [-36.85, 174.76, "Auckland, New Zealand"],
    [-41.29, 174.78, "Wellington, New Zealand"], [-43.53, 172.64, "Christchurch, New Zealand"],
  ];

  // ---------------------------------------------------------------- state

  function readJson(key, fallback) {
    try {
      const raw = window.localStorage.getItem(key);
      if (!raw) return fallback;
      const parsed = JSON.parse(raw);
      return parsed === null || parsed === undefined ? fallback : parsed;
    } catch (error) {
      return fallback;
    }
  }

  function writeJson(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      /* private mode: settings simply don't persist */
    }
  }

  let settings = Object.assign({}, DEFAULTS, readJson(SETTINGS_KEY, {}));
  let queue = readJson(QUEUE_KEY, {});        // buildingId -> {idle, addedAt, label}
  let history = readJson(HISTORY_KEY, []);    // [{lat, lng, id, at, type}]
  let cachedTypes = readJson(TYPES_KEY, []);  // [{value, label}]
  let session = readJson(SESSION_KEY, { count: 0, startedAt: 0, cursor: 0 });
  let needsDispatch = readJson(NEEDS_KEY, {});   // country -> times skipped

  if (!Array.isArray(settings.typeValues)) settings.typeValues = [];
  if (!settings.typeValues.length && settings.typeValue) {
    settings.typeValues = [String(settings.typeValue)];   // 1.x had one type
    delete settings.typeValue;
    delete settings.typeLabel;
    writeJson(SETTINGS_KEY, settings);
  }

  const state = {
    running: false,
    busy: false,
    lastError: "",
    nextAt: 0,
    nextFinishAt: 0,
    credits: null,
    logLines: [],
  };

  function saveSettings() { writeJson(SETTINGS_KEY, settings); }
  function saveQueue() { writeJson(QUEUE_KEY, queue); }
  function saveSession() { writeJson(SESSION_KEY, session); }
  function saveNeeds() { writeJson(NEEDS_KEY, needsDispatch); }
  function saveHistory() {
    history = history.slice(-2000);
    writeJson(HISTORY_KEY, history);
  }

  // ---------------------------------------------------------------- utils

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function log(message, kind) {
    const line = {
      at: new Date().toLocaleTimeString(),
      text: String(message),
      kind: kind || "info",
    };
    state.logLines.push(line);
    if (state.logLines.length > 300) state.logLines.shift();
    if (kind === "error") console.warn("[FRA Auto-Build]", message);
    else console.info("[FRA Auto-Build]", message);
    renderLog();
  }

  function distanceMeters(lat1, lng1, lat2, lng2) {
    const R = 6371000;
    const toRad = (deg) => (deg * Math.PI) / 180;
    const dLat = toRad(lat2 - lat1);
    const dLng = toRad(lng2 - lng1);
    const a =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  function offsetPoint(lat, lng, maxKm) {
    // Uniform inside the disc (sqrt on the radius), not clustered at the
    // centre — otherwise every "random" build lands on the same block.
    const distanceKm = maxKm * Math.sqrt(Math.random());
    const bearing = Math.random() * 2 * Math.PI;
    const dLat = (distanceKm * Math.cos(bearing)) / 111.0;
    const cosLat = Math.max(0.2, Math.cos((lat * Math.PI) / 180));
    const dLng = (distanceKm * Math.sin(bearing)) / (111.0 * cosLat);
    return [lat + dLat, lng + dLng];
  }

  function parseCoords(text) {
    const match = String(text || "").match(
      /^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$/
    );
    if (!match) return null;
    const lat = parseFloat(match[1]);
    const lng = parseFloat(match[2]);
    if (!isFinite(lat) || !isFinite(lng)) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    return [lat, lng];
  }

  function tooCloseToHistory(lat, lng) {
    for (const entry of history) {
      if (distanceMeters(lat, lng, entry.lat, entry.lng) < DUPLICATE_RADIUS_M) {
        return true;
      }
    }
    return false;
  }

  // ------------------------------------------------------------ tab owner

  function claimOwner(force) {
    const now = Date.now();
    const owner = readJson(OWNER_KEY, null);
    if (!force && owner && owner.tab !== TAB_ID
        && now - (owner.at || 0) < OWNER_STALE_MS) {
      return false;
    }
    writeJson(OWNER_KEY, { tab: TAB_ID, at: now });
    return true;
  }

  function releaseOwner() {
    const owner = readJson(OWNER_KEY, null);
    if (owner && owner.tab === TAB_ID) writeJson(OWNER_KEY, { tab: TAB_ID, at: 0 });
  }

  // -------------------------------------------------------- game adapters
  //
  // Everything goes through plain same-origin requests with your own
  // session and the page's CSRF token. Up to 1.6.0 the script drove the
  // pages in a hidden frame — but /buildings/new turned into a bare HTML
  // fragment (no layout, no jQuery, no map) that only works when the
  // game's own map page loads it. In a frame nothing on it runs, so not a
  // single build could go out. Requests don't care about any of that.

  function isLoginPage(doc) {
    return !!doc.querySelector('input[type="password"]');
  }

  function flashText(doc) {
    const el = doc.querySelector(
      ".alert-danger, .alert.alert-error, #flash_error, .flash_error, " +
      "#error_explanation, .danger.alert, .has-error .help-block"
    );
    if (!el) return "";
    return (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 300);
  }

  async function fetchPage(path, options) {
    const url = path.startsWith("http") ? path : BASE + path;
    const response = await fetch(url, Object.assign(
      { credentials: "same-origin", cache: "no-store" }, options || {}));
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    return { response, html, doc, url: response.url || url };
  }

  async function fetchDoc(path) {
    const page = await fetchPage(path);
    if (/\/users\/sign_(in|up)/.test(page.url) || isLoginPage(page.doc)) {
      throw new Error("you are logged out of MissionChief");
    }
    if (!page.response.ok) {
      throw new Error(path + " -> HTTP " + page.response.status);
    }
    return page.doc;
  }

  async function fetchJson(path) {
    const response = await fetch(BASE + path, {
      credentials: "same-origin", cache: "no-store",
    });
    if (!response.ok) throw new Error(path + " -> HTTP " + response.status);
    return response.json();
  }

  function csrfToken(doc) {
    const meta = (doc || document).querySelector('meta[name="csrf-token"]');
    if (meta && meta.getAttribute("content")) return meta.getAttribute("content");
    const field = (doc || document).querySelector('input[name="authenticity_token"]');
    if (field && field.value) return field.value;
    return doc && doc !== document ? csrfToken(document) : null;
  }

  async function send(path, params, token) {
    // A Rails form post, or a data-method="post" link, sent the way the
    // game's own jQuery sends it: url-encoded, XHR, CSRF token in both the
    // header and the body.
    const body = params instanceof URLSearchParams
      ? params : new URLSearchParams(params || {});
    if (token && !body.has("authenticity_token")) body.set("authenticity_token", token);
    const headers = {
      "X-Requested-With": "XMLHttpRequest",
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    };
    if (token) headers["X-CSRF-Token"] = token;
    return fetchPage(path, { method: "POST", headers, body: body.toString() });
  }

  function formParams(form) {
    // Exactly what jQuery's $(form).serialize() sends — which is how the
    // game submits its forms: every named, enabled control except buttons,
    // files and unticked boxes. NO submit-button value ever goes along.
    const params = new URLSearchParams();
    for (const field of form.elements) {
      if (!field.name || field.disabled) continue;
      const type = String(field.type || "").toLowerCase();
      if (["submit", "button", "image", "reset", "file"].includes(type)) continue;
      if ((type === "checkbox" || type === "radio") && !field.checked) continue;
      if (field.tagName === "SELECT" && field.multiple) {
        for (const option of field.selectedOptions) params.append(field.name, option.value);
        continue;
      }
      params.append(field.name, field.value === undefined ? "" : String(field.value));
    }
    return params;
  }

  async function readCredits() {
    // The game's own credit API first (always current); the navbar
    // counter (#navigation_top, "25,523,443") as a fallback.
    try {
      const data = await fetchJson("/api/credits");
      const value = parseInt(data && data.credits_user_current, 10);
      if (isFinite(value)) return value;
    } catch (error) {
      /* fall back to the navbar */
    }
    const el = document.querySelector("#navigation_top");
    if (!el) return null;
    const digits = (el.textContent || "").replace(/[^\d]/g, "");
    return digits ? parseInt(digits, 10) : null;
  }

  let ownBuildingsCache = { at: 0, rows: [] };

  async function ownBuildings(force) {
    if (!force && Date.now() - ownBuildingsCache.at < 120000) {
      return ownBuildingsCache.rows;
    }
    const rows = await fetchJson("/api/buildings");
    ownBuildingsCache = { at: Date.now(), rows: Array.isArray(rows) ? rows : [] };
    return ownBuildingsCache.rows;
  }

  function buildingCoords(row) {
    const lat = parseFloat(row.latitude !== undefined ? row.latitude : row.lat);
    const lng = parseFloat(
      row.longitude !== undefined ? row.longitude
        : row.lon !== undefined ? row.lon : row.lng
    );
    return isFinite(lat) && isFinite(lng) ? [lat, lng] : null;
  }

  // ------------------------------------------------------ address lookups

  async function osmPlace(lat, lng) {
    // Second opinion on "is this a real place": the game's own reverse
    // lookup is US-centric and returns nothing for a lot of the world.
    // Also where the country/region/city come from — the dispatch center
    // is chosen by NAME against those, not by distance.
    try {
      const response = await fetch(
        `${NOMINATIM_REVERSE}&lat=${lat.toFixed(6)}&lon=${lng.toFixed(6)}`,
        { headers: { Accept: "application/json" } }
      );
      if (!response.ok) return null;
      const data = await response.json();
      if (!data || data.error) return null;
      const a = data.address || {};
      if (!a.road && !a.city && !a.town && !a.village && !a.suburb) return null;
      return {
        name: (data.name || "").trim(),        // OSM's own name for the spot
        category: data.category || "",         // e.g. "amenity" / "highway"
        kind: data.type || "",                 // e.g. "police" / "motorway"
        address: (data.display_name || "").slice(0, 160),
        country: a.country || "",
        state: a.state || a.region || "",
        county: a.county || a.state_district || "",
        city: a.city || a.town || a.village || a.municipality || a.suburb || "",
      };
    } catch (error) {
      return null;
    }
  }

  async function osmAddress(lat, lng) {
    const place = await osmPlace(lat, lng);
    return place ? place.address : null;
  }

  // A building should be called what OSM calls it, and stand where OSM says
  // it stands. So before building, ask Overpass for a real facility of the
  // right kind near the spot and take BOTH its name and its coordinates.
  // Nothing found (or Overpass down) is not an error — the name then falls
  // back to OSM's name for the spot itself, and only then to a plain
  // "<town> <type>". No invented city names either way.
  const FACILITY_TAGS = [
    { re: /fire\s*station|feuerwache/i, tags: ['"amenity"="fire_station"'] },
    { re: /police/i, tags: ['"amenity"="police"'] },
    { re: /prison|jail/i, tags: ['"amenity"="prison"'] },
    { re: /hospital|clinic/i,
      tags: ['"amenity"="hospital"', '"healthcare"="hospital"'] },
    { re: /rescue|ambulance|ems/i, tags: ['"emergency"="ambulance_station"'] },
  ];

  function facilityTagsFor(typeLabel) {
    if (/academy|school|training/i.test(typeLabel)) return null;  // never snap
    const entry = FACILITY_TAGS.find((candidate) => candidate.re.test(typeLabel));
    return entry ? entry.tags : null;
  }

  function facilityQuery(lat, lng, tags, radiusM) {
    const parts = [];
    for (const tag of tags) {
      for (const kind of ["node", "way"]) {
        parts.push(`${kind}[${tag}](around:${radiusM},${lat.toFixed(5)},${lng.toFixed(5)});`);
      }
    }
    return `[out:json][timeout:20];(${parts.join("")});out tags center 40;`;
  }

  function placeIsFacility(typeLabel, place) {
    // Nominatim's name for a spot is only a building name when the spot IS
    // such a facility — otherwise it is a road ("Rijksweg A50"), a shop or
    // a park, and the town + type fallback is the honest name.
    const tags = facilityTagsFor(typeLabel);
    if (!tags || !place || !place.category) return false;
    return tags.some((tag) => {
      const match = tag.match(/^"([^"]+)"="([^"]+)"$/);
      return match && match[1] === place.category && match[2] === place.kind;
    });
  }

  // When every mirror is down or refusing us, asking again for every spot
  // only adds a long wait to each attempt. Skip Overpass for a while.
  let overpassDownUntil = 0;
  const OVERPASS_PAUSE_MS = 15 * 60 * 1000;

  async function overpass(query) {
    // All mirrors at once; the first good answer wins and the rest are
    // cancelled. One slow or dead mirror no longer costs its full timeout.
    const controllers = OVERPASS_URLS.map(() => new AbortController());
    const timer = setTimeout(
      () => controllers.forEach((controller) => controller.abort()),
      OVERPASS_TIMEOUT_MS);
    const errors = [];
    try {
      return await Promise.any(OVERPASS_URLS.map(async (url, index) => {
        try {
          const response = await fetch(url, {
            method: "POST",
            body: "data=" + encodeURIComponent(query),
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            signal: controllers[index].signal,
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const data = await response.json();
          controllers.forEach((controller, other) => {
            if (other !== index) controller.abort();
          });
          return data;
        } catch (error) {
          const host = url.replace(/^https?:\/\/([^/]+).*$/, "$1");
          errors.push(`${host}: ${error.name === "AbortError" ? "no answer" : error.message}`);
          throw error;
        }
      }));
    } catch (error) {
      throw new Error(errors.join(", ") || "no Overpass mirror answered");
    } finally {
      clearTimeout(timer);
    }
  }

  async function osmFacility(lat, lng, typeLabel) {
    const tags = facilityTagsFor(typeLabel);
    if (!tags) return null;
    if (Date.now() < overpassDownUntil) return null;
    const radius = Math.max(200, (Number(settings.facilityRadiusKm) || 3) * 1000);
    let data;
    try {
      data = await overpass(facilityQuery(lat, lng, tags, radius));
    } catch (error) {
      overpassDownUntil = Date.now() + OVERPASS_PAUSE_MS;
      log(`⚠️ OpenStreetMap facility lookup failed (${error.message}) — ` +
          "naming from the address instead; not asking Overpass again for 15 min",
          "error");
      return null;
    }
    const found = [];
    for (const element of (data && data.elements) || []) {
      const name = ((element.tags && element.tags.name) || "").trim();
      if (!name) continue;                       // an unnamed one tells us nothing
      const pointLat = element.lat !== undefined
        ? element.lat : (element.center && element.center.lat);
      const pointLng = element.lon !== undefined
        ? element.lon : (element.center && element.center.lon);
      if (!isFinite(pointLat) || !isFinite(pointLng)) continue;
      if (tooCloseToHistory(pointLat, pointLng)) continue;
      found.push({ name, lat: pointLat, lng: pointLng });
    }
    if (!found.length) return null;
    return found[Math.floor(Math.random() * found.length)];
  }

  async function osmGeocode(text) {
    const url =
      "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=" +
      encodeURIComponent(text);
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("OpenStreetMap search -> HTTP " + response.status);
    const rows = await response.json();
    if (!rows || !rows.length) throw new Error("no place found for " + text);
    return [parseFloat(rows[0].lat), parseFloat(rows[0].lon), rows[0].display_name];
  }

  // ------------------------------------------------------ the build form
  //
  // /buildings/new, read live (Nov 2026): form#new_building posts to
  // /buildings. Per type there is a block #detail_<type> holding
  //   - the personal button  #build_credits_<type>  ("Build 2,071,596 Credits")
  //   - a coins button       .coins_activate
  //   - alliance buttons     .alliance_activate (only some types)
  //   - for fire stations    building[start_vehicle_feuerwache(_kleinwache)]
  // and one dispatch select building[leitstelle_building_id] for all types
  // (the form hides it for a dispatch center and a prison).
  // The game sends the form with $(form).serialize() over XHR, so no button
  // value goes along: credits vs coins is decided ONLY by the hidden
  // build_with_coins field, which the coins button sets to 1. Left empty —
  // exactly as the game leaves it for a credits build — it is credits.

  const NO_DISPATCH_STEP_TYPES = ["1", "10"];   // the form hides the step

  function visibleText(element) {
    return [
      element && element.value,
      element && element.textContent,
      element && element.getAttribute && element.getAttribute("title"),
      element && element.getAttribute && element.getAttribute("aria-label"),
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  }

  function readTypeOptions(doc) {
    const select = doc.querySelector("#building_building_type")
      || doc.querySelector('[name="building[building_type]"]');
    if (!select) return [];
    return [...select.options]
      .filter((option) => option.value !== "")
      .map((option) => ({
        value: String(option.value),
        label: (option.textContent || "").replace(/\s+/g, " ").trim(),
      }));
  }

  function buildForm(doc) {
    return doc.querySelector("#new_building")
      || doc.querySelector('form[action$="/buildings"]');
  }

  function typeBlock(doc, typeValue) {
    return doc.getElementById("detail_" + typeValue);
  }

  function isDisabled(element) {
    return !!(element.disabled || element.hasAttribute("disabled")
      || element.classList.contains("disabled"));
  }

  function personalButton(doc, typeValue) {
    const block = typeBlock(doc, typeValue);
    const seen = block
      ? [...block.querySelectorAll('input[type="submit"], button')].map((button) =>
        visibleText(button)
        + (button.classList.contains("alliance_activate") ? " [alliance]" : "")
        + (button.classList.contains("coins_activate") ? " [coins]" : "")
        + (isDisabled(button) ? " [disabled]" : ""))
      : [];
    const button = doc.getElementById("build_credits_" + typeValue);
    if (!button) {
      return { ok: false, reason: `no personal credits button for type ${typeValue}`, seen };
    }
    const label = visibleText(button);
    if (button.classList.contains("coins_activate")
        || button.classList.contains("alliance_activate")
        || !/credits/i.test(label) || /coin/i.test(label)) {
      return { ok: false, reason: `unexpected build button "${label}"`, seen };
    }
    if (isDisabled(button) || button.classList.contains("btn-danger")) {
      return { ok: false, reason: `the game will not build this with credits ("${label}")`, seen };
    }
    return { ok: true, label, cost: costOf(label), seen };
  }

  function costOf(label) {
    // "Build 2,071,596 Credits" / "Expand (60,000 Credits)" -> number
    const match = String(label || "").match(/([\d][\d.,\s]*)\s*credits/i);
    if (!match) return null;
    const digits = match[1].replace(/[^\d]/g, "");
    return digits ? parseInt(digits, 10) : null;
  }

  function startingVehicleSelect(doc, typeValue) {
    const block = typeBlock(doc, typeValue);
    if (!block) return null;
    return block.querySelector('select[name^="building[start_vehicle"]')
      || block.querySelector("select");
  }

  function pickStartingVehicle(doc, typeValue, wanted) {
    const needle = String(wanted || "").trim().toLowerCase();
    if (!needle) return { applied: false, reason: "no preference set" };
    const select = startingVehicleSelect(doc, typeValue);
    if (!select) {
      return { applied: false, reason: "this building type has no starting-vehicle choice" };
    }
    const options = [...select.options].filter((option) => option.value !== "");
    const text = (option) => (option.textContent || "").replace(/\s+/g, " ").trim();
    const match = options.find((option) => text(option).toLowerCase().includes(needle));
    if (!match) {
      return {
        applied: false,
        reason: `"${wanted}" is not offered`,
        offered: options.map(text).join(" | ").slice(0, 400),
      };
    }
    return { applied: true, field: select.name, value: match.value, label: text(match) };
  }

  function buildDispatchSelect(doc) {
    return doc.querySelector('select[name="building[leitstelle_building_id]"]');
  }

  // ------------------------------------------------------ dispatch centers
  //
  // NOT "the nearest one": the dispatch centers are fixed per region, some
  // countries have several and some have none at all. So a building is
  // matched to a center by NAME against the country / region / city of the
  // spot, with an explicit rule list on top for the cases where the name
  // does not say it. A country with no center is never built in — it goes
  // on the "needs a dispatch center" list in the panel to ask about.

  const DISPATCH_TYPE_ID = 1;   // the game's own type id for a dispatch center

  function normalize(text) {
    return String(text || "")
      .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
      .toLowerCase().replace(/\s+/g, " ").trim();
  }

  function parseDispatchRules(text) {
    const rules = [];
    for (const line of String(text || "").split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const split = trimmed.indexOf("=");
      if (split < 1) continue;
      const pattern = normalize(trimmed.slice(0, split));
      const name = normalize(trimmed.slice(split + 1));
      if (pattern && name) rules.push({ pattern, name, raw: trimmed });
    }
    return rules;
  }

  async function dispatchCenters() {
    const rows = await ownBuildings(false);
    const named = (row) => String(row.caption || row.name || "").trim();
    let centers = rows.filter((row) => parseInt(
      row.building_type !== undefined ? row.building_type : row.building_type_id, 10
    ) === DISPATCH_TYPE_ID);
    if (!centers.length) {
      // Fall back to the name when the type id is not what we think it is.
      centers = rows.filter((row) => /dispatch|leitstelle/i.test(named(row)));
    }
    return centers.map((row) => {
      const coords = buildingCoords(row);
      return {
        id: String(row.id),
        name: named(row),
        lat: coords ? coords[0] : null,
        lng: coords ? coords[1] : null,
      };
    }).filter((center) => center.name);
  }

  function matchDispatch(place, centers, rules) {
    if (!centers.length) {
      return { ok: false, reason: "you have no dispatch centers at all" };
    }
    const fields = [
      ["city", place.city], ["county", place.county],
      ["region", place.state], ["country", place.country],
    ].filter((entry) => entry[1]);
    const haystack = normalize(
      [place.city, place.county, place.state, place.country, place.address]
        .filter(Boolean).join(" | ")
    );
    for (const rule of rules) {
      if (!haystack.includes(rule.pattern)) continue;
      const center = centers.find((candidate) =>
        normalize(candidate.name).includes(rule.name));
      if (center) return { ok: true, center, why: `rule "${rule.raw}"` };
      return {
        ok: false,
        reason: `rule "${rule.raw}" matches, but you have no dispatch center ` +
                `whose name contains "${rule.name}"`,
      };
    }
    for (const [what, value] of fields) {           // city first, country last
      const needle = normalize(value);
      if (!needle) continue;
      const center = centers.find((candidate) => {
        const name = normalize(candidate.name);
        return name.includes(needle) || needle.includes(name);
      });
      if (center) {
        return { ok: true, center, why: `${what} "${value}" is in its name` };
      }
    }
    return {
      ok: false,
      reason: `no dispatch center for ${place.country || "that place"}` +
              (place.state ? ` / ${place.state}` : ""),
      country: place.country || "(unknown country)",
    };
  }

  function noteMissingDispatch(country, reason) {
    const key = country || "(unknown country)";
    const seen = needsDispatch[key] || 0;
    needsDispatch[key] = seen + 1;
    saveNeeds();
    if (!seen) log(`❓ ${reason} — skipped. Tell me which center to use for ` +
                   `${key}, or add a rule in "Dispatch rules".`, "error");
    renderNeeds();
  }

  // ------------------------------------------------------------- location

  function pickAnchor() {
    const mode = settings.locationMode;
    const useArea = mode === "area"
      || (mode === "mix" && Math.random() < 0.5);
    if (useArea && settings.areaLat !== null && settings.areaLng !== null) {
      return {
        lat: settings.areaLat,
        lng: settings.areaLng,
        label: settings.areaText || "your area",
        radiusKm: Math.max(0.5, Number(settings.areaRadiusKm) || 25),
      };
    }
    const point = WORLD_POINTS[Math.floor(Math.random() * WORLD_POINTS.length)];
    return {
      lat: point[0],
      lng: point[1],
      label: point[2],
      radiusKm: Math.max(0.5, Number(settings.jitterKm) || 12),
    };
  }

  async function pickLocation(typeLabel) {
    // Returns a spot that is not a duplicate, has a street address, and —
    // when dispatch linking is on — sits in a region one of your dispatch
    // centers covers. A spot that fails the last test is skipped and its
    // country noted, not built. When OSM knows a real facility of this kind
    // nearby, the spot MOVES onto it and takes its name.
    const centers = settings.linkDispatch ? await dispatchCenters() : [];
    const rules = parseDispatchRules(settings.dispatchRules);
    const needsPlace = settings.verifyWithOsm || settings.linkDispatch
      || settings.useOsmNames;
    let lastReason = "";
    for (let attempt = 0; attempt < MAX_LOCATION_TRIES; attempt++) {
      const anchor = pickAnchor();
      let [lat, lng] = offsetPoint(anchor.lat, anchor.lng, anchor.radiusKm);
      if (tooCloseToHistory(lat, lng)) {
        lastReason = "too close to something this script already built";
        continue;
      }
      let osmName = "";
      if (settings.useOsmNames) {
        const facility = await osmFacility(lat, lng, typeLabel);
        if (facility) {
          lat = facility.lat;
          lng = facility.lng;
          osmName = facility.name;
        }
      }
      if (!needsPlace) {
        return { lat, lng, label: anchor.label, address: null, place: null,
                 dispatch: null, osmName };
      }
      const place = await osmPlace(lat, lng);
      if (!place) {
        lastReason = "no street address there (water/desert)";
        await sleep(1100);   // Nominatim asks for at most one call per second
        continue;
      }
      if (!osmName && place.name && placeIsFacility(typeLabel, place)) {
        osmName = place.name;            // the spot itself is such a facility
      }
      if (!settings.linkDispatch) {
        return { lat, lng, label: anchor.label, address: place.address, place,
                 dispatch: null, osmName };
      }
      const match = matchDispatch(place, centers, rules);
      if (!match.ok) {
        noteMissingDispatch(match.country, match.reason);
        lastReason = match.reason;
        await sleep(1100);
        continue;
      }
      return {
        lat, lng, label: anchor.label, address: place.address, place,
        dispatch: match.center, dispatchWhy: match.why, osmName,
      };
    }
    throw new Error(
      `could not find a usable spot in ${MAX_LOCATION_TRIES} tries — ${lastReason}`
    );
  }

  function knownTypes() {
    return cachedTypes.length
      ? cachedTypes
      : (settings.typeValues || []).map((value) => ({ value, label: value }));
  }

  function selectedTypes() {
    const wanted = new Set((settings.typeValues || []).map(String));
    return knownTypes().filter((type) => wanted.has(String(type.value)));
  }

  function nextType() {
    // Round robin, not random: "all buildings" should come out evenly
    // spread, not thirty fire stations and one prison.
    const types = selectedTypes();
    if (!types.length) return null;
    const cursor = (session.cursor || 0) % types.length;
    session.cursor = cursor + 1;
    saveSession();
    return types[cursor];
  }

  function buildingName(typeLabel, spot) {
    // OSM's own name wins, verbatim. Only when OSM has no name for the
    // place does this fall back to "<town> <type>" — and then the town is
    // the one OSM reports for the ACTUAL spot, never the city this script
    // aimed at, which can be tens of kilometres away.
    const fromOsm = String((spot && spot.osmName) || "").replace(/\s+/g, " ").trim();
    if (fromOsm) return fromOsm.slice(0, 40);
    const place = spot && spot.place;
    const town = (place && (place.city || place.county || place.state)) || "";
    const name = `${town || "New"} ${typeLabel}`.replace(/\s+/g, " ").trim();
    return name.slice(0, 40);   // the game's name limit
  }

  // ---------------------------------------------------------- build a one

  async function gameAddress(lat, lng) {
    // The game's own pin lookup — the same call its map makes when you drop
    // the pin. Plain text ("Bannock Central Plaza,  Denver"); empty at sea.
    try {
      const response = await fetch(
        `${BASE}/reverse_address?latitude=${lat.toFixed(6)}&longitude=${lng.toFixed(6)}`,
        { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } }
      );
      if (!response.ok) return "";
      const text = (await response.text()).replace(/\s+/g, " ").trim();
      if (!text || /<[a-z!]/i.test(text)) return "";   // empty, or an HTML page
      return text.slice(0, 200);
    } catch (error) {
      return "";
    }
  }

  async function buildOne() {
    const type = nextType();
    if (!type) throw new Error("STOP — tick at least one building type first");
    const typeLabel = type.label || "building";
    const typeValue = String(type.value);
    const spot = await pickLocation(typeLabel);
    log(`📍 ${typeLabel}: ${spot.lat.toFixed(5)}, ${spot.lng.toFixed(5)} ` +
        `near ${spot.label}` +
        (spot.osmName ? ` — OSM: "${spot.osmName}"` : "") +
        (spot.dispatch ? ` → dispatch "${spot.dispatch.name}" (${spot.dispatchWhy})` : ""));

    const doc = await fetchDoc("/buildings/new");
    const form = buildForm(doc);
    if (!form) {
      throw new Error("STOP — the build form (/buildings/new) has no #new_building any more");
    }
    const options = readTypeOptions(doc);
    if (options.length) {
      cachedTypes = options;
      writeJson(TYPES_KEY, cachedTypes);
    }
    if (!options.some((option) => option.value === typeValue)) {
      throw new Error(`the game does not offer building type ${typeValue} (${typeLabel})`);
    }
    const button = personalButton(doc, typeValue);
    if (!button.ok) {
      const seen = button.seen.length ? ` Buttons seen: ${button.seen.join(" / ")}` : "";
      throw new Error(`${typeLabel}: ${button.reason}.${seen}`);
    }

    const name = buildingName(typeLabel, spot);
    let address = await gameAddress(spot.lat, spot.lng);
    let addressFrom = "MissionChief";
    if (!address) {
      address = spot.address || (await osmAddress(spot.lat, spot.lng)) || "";
      addressFrom = address ? "OpenStreetMap" : "nowhere";
    }
    if (!address) {
      throw new Error(
        `no address for ${spot.lat.toFixed(5)},${spot.lng.toFixed(5)} — skipped`
      );
    }

    const wantsVehicle = /fire\s*station|feuerwache/i.test(typeLabel)
      ? settings.startingVehicle
      : "";
    let vehicle = { applied: false, reason: "not a fire station" };
    if (wantsVehicle) {
      vehicle = pickStartingVehicle(doc, typeValue, wantsVehicle);
      if (!vehicle.applied && settings.strictVehicle) {
        const extra = vehicle.offered ? ` Offered: ${vehicle.offered}` : "";
        throw new Error(
          `STOP — the starting vehicle "${wantsVehicle}" could not be selected ` +
          `(${vehicle.reason}).${extra}`
        );
      }
    }

    // The dispatch center goes along with the build itself; the edit step
    // afterwards only checks it (and sets it when this did not stick).
    let dispatch = null;
    const dispatchField = buildDispatchSelect(doc);
    if (settings.linkDispatch && spot.dispatch && dispatchField
        && !NO_DISPATCH_STEP_TYPES.includes(typeValue)) {
      const option = optionForCenter(dispatchField, spot.dispatch);
      if (option) dispatch = { value: option.value, label: visibleText(option) };
      else log(`⚠️ "${spot.dispatch.name}" is not in the build form's dispatch list — ` +
               "the edit step will try again", "error");
    }

    const params = formParams(form);
    params.set("building[building_type]", typeValue);
    params.set("building[name]", name);
    params.set("building[latitude]", spot.lat.toFixed(6));
    params.set("building[longitude]", spot.lng.toFixed(6));
    params.set("building[address]", address);
    params.set("build_with_coins", "");     // credits — as the game sends it
    params.set("build_as_alliance", "");    // personal — as the game sends it
    params.delete("build_another");
    if (vehicle.applied) params.set(vehicle.field, vehicle.value);
    if (dispatch) params.set(dispatchField.name, dispatch.value);
    if (params.get("build_with_coins") !== "" || params.get("build_as_alliance") !== "") {
      throw new Error("refusing to build: the coins/alliance fields are not empty");
    }

    const summary = `"${name}" (${typeLabel}, ${button.label}` +
      (vehicle.applied ? `, start vehicle ${vehicle.label}` : "") +
      (dispatch ? `, dispatch ${dispatch.label}` : "") +
      `, address via ${addressFrom}: ${address})`;

    const credits = await readCredits();
    if (credits !== null && button.cost !== null
        && credits - button.cost < (Number(settings.creditsFloor) || 0)) {
      throw new Error(`STOP — building ${summary} would take credits below the floor ` +
        `(${credits.toLocaleString()} - ${button.cost.toLocaleString()} < ` +
        `${Number(settings.creditsFloor).toLocaleString()})`);
    }

    if (settings.dryRun) {
      log(`🧪 DRY RUN — would build ${summary}`, "ok");
      return { dryRun: true };
    }

    const before = new Set((await ownBuildings(true)).map((row) => String(row.id)));
    const sent = await send(form.getAttribute("action") || "/buildings", params,
      csrfToken(doc));
    if (/\/users\/sign_(in|up)/.test(sent.url) || isLoginPage(sent.doc)) {
      throw new Error("you are logged out of MissionChief");
    }
    if (!sent.response.ok) {
      const flash = flashText(sent.doc);
      throw new Error(`the game refused the build — HTTP ${sent.response.status}` +
        (flash ? `: ${flash}` : ""));
    }
    let buildingId = await confirmByApi(spot.lat, spot.lng, before);
    if (!buildingId) {
      await sleep(2500);    // the API can lag a moment behind the build
      buildingId = await confirmByApi(spot.lat, spot.lng, before);
    }
    if (!buildingId) {
      const flash = flashText(sent.doc);
      throw new Error(
        "the build did not confirm — no new building of yours at that spot" +
        (flash ? `: ${flash}` : " (the game showed no error)")
      );
    }

    history.push({
      id: buildingId, lat: spot.lat, lng: spot.lng,
      type: typeLabel, at: new Date().toISOString(),
    });
    saveHistory();
    log(`✅ built #${buildingId} ${summary}`, "ok");
    return { buildingId, name, lat: spot.lat, lng: spot.lng, spot,
             dispatchSet: !!dispatch };
  }

  async function confirmByApi(lat, lng, before) {
    // Ask the API for a building of ours at the pin that was not there
    // before the build. (The build's own reply is the sidebar HTML, which
    // names many buildings — it cannot say which one is new.)
    try {
      const rows = await ownBuildings(true);
      let best = null;
      let bestDistance = 300;
      for (const row of rows) {
        if (before && before.has(String(row.id))) continue;
        const coords = buildingCoords(row);
        if (!coords) continue;
        const distance = distanceMeters(lat, lng, coords[0], coords[1]);
        if (distance < bestDistance) {
          bestDistance = distance;
          best = row;
        }
      }
      return best ? parseInt(best.id, 10) : null;
    } catch (error) {
      return null;
    }
  }

  // ------------------------------------------------- link a dispatch center

  function dispatchSelect(doc) {
    const direct = doc.querySelector(
      'select[name="building[leitstelle_building_id]"], ' +
      'select[name*="leitstelle"], select[id*="leitstelle"], ' +
      'select[name*="dispatch"], select[id*="dispatch"]'
    );
    if (direct) return direct;
    return [...doc.querySelectorAll("select")].find((select) => {
      const label = visibleText(select.closest("div, td, li, form") || select)
        .toLowerCase();
      return label.includes("dispatch center") || label.includes("leitstelle");
    }) || null;
  }

  function optionForCenter(select, center) {
    const options = [...select.options].filter((option) => option.value !== "");
    const byId = options.find((option) => String(option.value) === String(center.id));
    if (byId) return byId;
    // The option values are not building ids on this page: go by name.
    const wanted = normalize(center.name);
    return options.find((option) => normalize(visibleText(option)) === wanted)
      || options.find((option) => normalize(visibleText(option)).includes(wanted))
      || null;
  }

  // --------------------------------------------------- deliver a building
  //
  // Deliberately NOT "buy everything the page offers": extensions are OFF
  // by default. A new building gets its LEVEL raised as far as it goes and
  // every STORAGE slot bought — nothing else, unless you switch extensions
  // on yourself. Read live (Nov 2026):
  //   level      GET  /buildings/<id>/expand_do/credits?level=<n>  — listed
  //              on /buildings/<id>/expand, one link per target level, the
  //              price already summed, so the highest link is "max level"
  //   storage    POST /buildings/<id>/storage_upgrade/credits/<slug>
  //   extension  POST /buildings/<id>/extension/credits/<n>
  // Storage rooms and extensions take days to build; the next one is shown
  // with class "disabled" until then. Those are left for the finish list.
  // Coin links (…/coins/…, data-confirm) are never even looked at.

  const LEVEL_HREF_RE = /\/expand_do\/credits\b/i;
  const STORAGE_HREF_RE = /\/storage_upgrade\/credits\//i;
  const EXTENSION_HREF_RE = /\/extension\/credits\/\d+/i;
  const STAFF_NAME_RE = /personal|personnel|staff|crew|besetzung/i;
  const MAX_DELIVERY_STEPS = 40;

  function purchaseLinks(doc, buildingId, hrefRe) {
    const prefix = `/buildings/${buildingId}/`;
    const links = [];
    for (const anchor of doc.querySelectorAll("a[href]")) {
      let href = anchor.getAttribute("href") || "";
      if (href.startsWith(BASE)) href = href.slice(BASE.length);
      const label = visibleText(anchor);
      if (!href.startsWith(prefix) || !hrefRe.test(href)) continue;
      if (/coin/i.test(`${href} ${label}`) || anchor.hasAttribute("data-confirm")) continue;
      links.push({
        href,
        label,
        cost: costOf(label),
        disabled: isDisabled(anchor),
        method: (anchor.getAttribute("data-method") || "get").toLowerCase(),
      });
    }
    return links;
  }

  function levelOf(href) {
    const match = String(href).match(/[?&]level=(\d+)/);
    return match ? parseInt(match[1], 10) : -1;
  }

  async function buy(link, token) {
    const page = link.method === "get"
      ? await fetchPage(link.href, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      : await send(link.href, {}, token);
    if (/\/users\/sign_(in|up)/.test(page.url) || isLoginPage(page.doc)) {
      throw new Error("you are logged out of MissionChief");
    }
    return page;
  }

  async function deliverBuilding(buildingId, entry) {
    // entry (optional): the finish-list record, which remembers how many
    // storage rooms were already bought across visits.
    const counts = { level: 0, storage: 0, extension: 0 };
    const labels = [];
    const problems = [];
    let waiting = 0;
    const dry = !!settings.dryRun;
    const floor = Number(settings.creditsFloor) || 0;
    let credits = await readCredits();
    const affordable = (cost) =>
      credits === null || cost === null || credits - cost >= floor;
    const spend = (cost) => {
      if (credits !== null && cost !== null) credits -= cost;
    };

    if (settings.maxLevel) {
      const doc = await fetchDoc(`/buildings/${buildingId}/expand`);
      const levels = purchaseLinks(doc, buildingId, LEVEL_HREF_RE)
        .filter((link) => !link.disabled && affordable(link.cost))
        .sort((a, b) => levelOf(b.href) - levelOf(a.href));
      if (levels.length) {
        const top = levels[0];
        counts.level = 1;
        labels.push(`level ${levelOf(top.href) + 1} (${top.label})`);
        if (!dry) {
          const result = await buy(top, csrfToken(doc));
          if (!result.response.ok) {
            problems.push(`level: HTTP ${result.response.status}`);
          } else {
            spend(top.cost);
          }
        }
      } else if (purchaseLinks(doc, buildingId, LEVEL_HREF_RE).length) {
        problems.push("level: not affordable above the credit floor");
      }
    }

    const kinds = [];
    if (settings.buyStorage) {
      const done = (entry && entry.storageBought) || 0;
      kinds.push(["storage", STORAGE_HREF_RE,
        Math.max(0, (parseInt(settings.maxStorageBuys, 10) || 0) - done)]);
    }
    if (settings.buyExtensions) kinds.push(["extension", EXTENSION_HREF_RE, Infinity]);
    if (kinds.length) {
      let doc = await fetchDoc(`/buildings/${buildingId}`);
      for (const [kind, hrefRe, cap] of kinds) {
        const attempted = new Set();
        for (let step = 0; step < MAX_DELIVERY_STEPS; step++) {
          const links = purchaseLinks(doc, buildingId, hrefRe)
            .filter((link) => !attempted.has(link.href));
          const open = links.filter((link) => !link.disabled);
          if (counts[kind] >= cap) break;
          if (!open.length) {
            waiting += links.length;      // greyed out: still building
            break;
          }
          const next = open[0];
          attempted.add(next.href);       // each slug is a one-time purchase
          if (!affordable(next.cost)) {
            problems.push(`${kind}: ${next.label} not affordable above the credit floor`);
            waiting += 1;
            break;
          }
          counts[kind] += 1;
          labels.push(`${kind} ${next.label}`.slice(0, 60));
          if (dry) continue;
          const result = await buy(next, csrfToken(doc));
          if (!result.response.ok) {
            problems.push(`${kind}: HTTP ${result.response.status}`);
            counts[kind] -= 1;
            break;
          }
          spend(next.cost);
          await sleep(800);
          doc = await fetchDoc(`/buildings/${buildingId}`);
        }
      }
    }
    if (entry && !dry) entry.storageBought = (entry.storageBought || 0) + counts.storage;
    return { counts, labels: labels.slice(0, 12), waiting, problems };
  }

  // The building's own settings live on /buildings/<id>/edit: the staff
  // target (building[personal_count_target], the second box on the form)
  // and the dispatch center. Both are set in one request — two separate
  // saves would each write the other field's old value back.

  function staffFieldOnEdit(form) {
    const direct = form.querySelector('[name="building[personal_count_target]"]');
    if (direct) return direct;
    const texts = [...form.querySelectorAll(
      'input[type="text"], input[type="number"], input:not([type])'
    )].filter((field) => field.name);
    return texts.find((field) => STAFF_NAME_RE.test(field.name.toLowerCase()))
      || texts[1] || null;
  }

  function editForm(doc, buildingId) {
    return doc.querySelector(`form[action$="/buildings/${buildingId}"]`)
      || doc.querySelector('form[action*="/buildings/"]');
  }

  async function editBuilding(buildingId, center) {
    // Returns {dispatch: {...}, staff: {...}} — each either applied,
    // already right, skipped or refused, with a reason.
    const report = { dispatch: null, staff: null };
    const wantStaff = !!settings.setStaffLimit;
    const wantDispatch = !!(settings.linkDispatch && center);
    if (!wantStaff && !wantDispatch) return report;

    let doc;
    try {
      doc = await fetchDoc(`/buildings/${buildingId}/edit`);
    } catch (error) {
      if (/logged out/.test(error.message)) throw error;
      return {
        dispatch: wantDispatch ? { ok: false, reason: error.message } : null,
        staff: wantStaff ? { ok: false, reason: error.message } : null,
      };
    }
    const form = editForm(doc, buildingId);
    if (!form) {
      const failure = { ok: false, reason: "no form on the edit page" };
      return { dispatch: wantDispatch ? failure : null, staff: wantStaff ? failure : null };
    }

    let dirty = false;

    if (wantDispatch) {
      const select = dispatchSelect(form);
      if (!select) {
        report.dispatch = { ok: false, reason: "no dispatch field on the edit page" };
      } else {
        const option = optionForCenter(select, center);
        if (!option) {
          report.dispatch = {
            ok: false,
            reason: `"${center.name}" is not in the dispatch list (options: ` +
              [...select.options].map((entry) => visibleText(entry))
                .join(" | ").slice(0, 200) + ")",
          };
        } else if (String(select.value) === String(option.value)) {
          report.dispatch = { ok: true, already: true, label: center.name };
        } else if (settings.dryRun) {
          report.dispatch = { ok: true, dryRun: true, label: center.name };
        } else {
          select.value = String(option.value);
          dirty = true;
          report.dispatch = { ok: true, label: center.name };
        }
      }
    }

    if (wantStaff) {
      const field = staffFieldOnEdit(form);
      if (!field) {
        report.staff = { ok: false, reason: "no staff field on the edit page" };
      } else {
        const target = Math.max(0, parseInt(settings.staffLimit, 10) || 0);
        const max = parseInt(field.getAttribute("max") || "", 10);
        const value = isFinite(max) && max > 0 ? Math.min(target, max) : target;
        const current = parseInt(String(field.value).replace(/[^\d]/g, ""), 10);
        const where = field.name || field.id || "(unnamed)";
        if (isFinite(current) && current === value) {
          report.staff = { ok: true, already: true, value: current, field: where };
        } else if (settings.dryRun) {
          report.staff = { ok: true, dryRun: true, value, was: field.value, field: where };
        } else {
          field.value = String(value);
          dirty = true;
          report.staff = { ok: true, value, was: current, field: where };
        }
      }
    }

    if (!dirty) return report;
    const sent = await send(form.getAttribute("action"), formParams(form), csrfToken(doc));
    const flash = flashText(sent.doc);
    if (!sent.response.ok || flash) {
      const failure = {
        ok: false,
        reason: `the game did not save it (HTTP ${sent.response.status}` +
          (flash ? `: ${flash}` : "") + ")",
      };
      if (report.dispatch && report.dispatch.ok && !report.dispatch.already) {
        report.dispatch = failure;
      }
      if (report.staff && report.staff.ok && !report.staff.already) report.staff = failure;
    }
    return report;
  }

  function describeEdit(report) {
    const parts = [];
    const say = (what, entry) => {
      if (!entry) return;
      if (entry.ok) {
        parts.push(`${what} ${entry.label || entry.value}` +
          (entry.dryRun ? " [dry run]" : entry.already ? " (already set)" : ""));
      } else {
        parts.push(`${what} FAILED — ${entry.reason}`);
      }
    };
    say("dispatch", report.dispatch);
    say("staff", report.staff);
    return parts.join(" · ");
  }

  function editOk(report) {
    return (!report.dispatch || report.dispatch.ok)
      && (!report.staff || report.staff.ok);
  }

  function describeDelivery(delivered) {
    return `level ${delivered.counts.level}, storage ${delivered.counts.storage}` +
      (settings.buyExtensions ? `, extensions ${delivered.counts.extension}` : "") +
      (delivered.labels.length ? ` — ${delivered.labels.join(", ")}` : "") +
      (delivered.waiting ? ` · ${delivered.waiting} still under construction/locked` : "") +
      (delivered.problems.length ? ` · ⚠️ ${delivered.problems.join("; ")}` : "");
  }

  async function afterBuild(result) {
    // Staff + dispatch, then level/storage, then onto the finish list.
    const center = result.spot && result.spot.dispatch;
    let settled = true;
    try {
      const edited = await editBuilding(result.buildingId, center);
      const described = describeEdit(edited);
      if (described) {
        log(`🛰️ #${result.buildingId}: ${described}`, editOk(edited) ? "info" : "error");
      }
      settled = editOk(edited);
    } catch (error) {
      log(`⚠️ #${result.buildingId}: edit page failed — ${error.message}`, "error");
      settled = false;
    }
    const entry = enqueue(result.buildingId, result.name, center, settled);
    try {
      const delivered = await deliverBuilding(result.buildingId, entry);
      log(`🧱 #${result.buildingId}: ${settings.dryRun ? "would do" : "done"} — ` +
          describeDelivery(delivered));
    } catch (error) {
      log(`⚠️ #${result.buildingId}: delivery failed — ${error.message}`, "error");
    }
    saveQueue();
  }

  // ------------------------------------------------------- the finish list

  function enqueue(buildingId, label, center, editDone) {
    const entry = {
      label: label || "",
      idle: 0,
      addedAt: Date.now(),
      nextAt: Date.now() + FINISH_INTERVAL_MS,
      // A dispatch link or a staff amount that did not stick is retried by
      // the finisher, exactly like the bot retries a building's tax.
      editDone: !!editDone,
      storageBought: 0,
      center: center ? { id: center.id, name: center.name } : null,
    };
    queue[String(buildingId)] = entry;
    saveQueue();
    return entry;
  }

  async function finishPass() {
    const now = Date.now();
    for (const [key, entry] of Object.entries(queue)) {
      if ((entry.nextAt || 0) > now) continue;
      const buildingId = parseInt(key, 10);
      let progressed = false;
      if (!entry.editDone && (entry.editTries || 0) < EDIT_MAX_TRIES) {
        entry.editTries = (entry.editTries || 0) + 1;
        try {
          const edited = await editBuilding(buildingId, entry.center);
          const described = describeEdit(edited);
          if (described) {
            log(`🛰️ #${buildingId}: ${described}`,
                editOk(edited) ? "info" : "error");
          }
          if (editOk(edited) && !settings.dryRun) {
            entry.editDone = true;
            progressed = true;
          }
        } catch (error) {
          log(`⚠️ #${buildingId}: edit page failed — ${error.message}`, "error");
        }
        if (!entry.editDone && entry.editTries >= EDIT_MAX_TRIES) {
          // Never nag forever: say it once and let the building retire.
          log(`🛑 #${buildingId}: giving up on the dispatch/staff settings ` +
              `after ${EDIT_MAX_TRIES} tries — set them by hand`, "error");
        }
      }
      let result;
      try {
        result = await deliverBuilding(buildingId, entry);
      } catch (error) {
        log(`⚠️ finish #${buildingId}: ${error.message}`, "error");
        if (/logged out/.test(error.message)) throw error;
        entry.nextAt = now + FINISH_INTERVAL_MS;
        saveQueue();
        return;
      }
      const bought = settings.dryRun ? 0 : result.counts.level
        + result.counts.storage + result.counts.extension;
      if (bought > 0 || result.problems.length) {
        log(`🏗️ #${buildingId}: ${describeDelivery(result)}`,
            result.problems.length ? "error" : "info");
      }
      entry.idle = (bought > 0 || progressed) ? 0 : (entry.idle || 0) + 1;
      entry.nextAt = now + (result.waiting ? FINISH_WAIT_INTERVAL_MS : FINISH_INTERVAL_MS);
      const editSettled = entry.editDone || (entry.editTries || 0) >= EDIT_MAX_TRIES;
      const tooOld = now - (entry.addedAt || now) > FINISH_MAX_AGE_MS;
      if ((entry.idle >= FINISH_IDLE_LIMIT && editSettled && !result.waiting) || tooOld) {
        delete queue[key];
        log(`🏁 #${buildingId} is ${tooOld ? "too old to keep waiting for" : "fully delivered"}` +
            " — off the finish list", "ok");
      }
      saveQueue();
      return;   // one building per pass keeps the traffic gentle
    }
  }

  // --------------------------------------------------------------- the loop

  async function creditsOk() {
    state.credits = await readCredits();
    if (state.credits === null) return true;   // unreadable: don't block
    return state.credits >= (Number(settings.creditsFloor) || 0);
  }

  async function tick() {
    if (!state.running || state.busy) return;
    if (!claimOwner()) {
      // Another tab was told to start: that one drives, this one steps back.
      log("another browser tab took the builder over — stopped here", "error");
      state.running = false;
      renderStatus();
      return;
    }
    if (Date.now() < state.nextAt) return;
    state.busy = true;
    try {
      await finishPass();
      if (session.count >= (Number(settings.maxPerSession) || 0)) {
        log(`⏹️ session cap reached (${settings.maxPerSession}) — stopped`, "ok");
        stop();
        return;
      }
      if (!selectedTypes().length) {
        log("⏹️ tick at least one building type first — stopped", "error");
        stop();
        return;
      }
      if (!(await creditsOk())) {
        log(`⏹️ credits ${state.credits.toLocaleString()} are at or below ` +
            `the floor ${Number(settings.creditsFloor).toLocaleString()} — stopped`,
            "error");
        stop();
        return;
      }
      const result = await buildOne();
      session.count += 1;
      saveSession();
      state.lastError = "";
      if (result.buildingId) await afterBuild(result);
    } catch (error) {
      state.lastError = error.message;
      log(`❌ ${error.message}`, "error");
      // A hard stop (a missing Quint, a logged-out session, a changed build
      // form) must not be retried in a loop — those repeat forever.
      if (/^STOP —|logged out/.test(error.message)) stop();
    } finally {
      const wait = Math.max(20, Number(settings.intervalSeconds) || 90);
      state.nextAt = Date.now() + wait * 1000 + Math.random() * 5000;
      state.busy = false;
      renderStatus();
    }
  }

  function start() {
    claimOwner(true);          // a deliberate click wins over an idle tab
    state.running = true;
    settings.enabled = true;
    saveSettings();
    session = { count: 0, startedAt: Date.now(), cursor: 0 };
    saveSession();
    state.nextAt = 0;
    const picked = selectedTypes();
    log(`▶️ started — ${settings.dryRun ? "DRY RUN" : "LIVE"}, ` +
        `${picked.length ? picked.map((type) => type.label).join(" + ") : "no types!"}` +
        `, mode ${settings.locationMode}`, "ok");
    renderStatus();
  }

  function resume() {
    // The toggle is meant to stay on across page loads (clicking around
    // the game reloads this script). The session counter is NOT reset, so
    // "max this run" still means what it says.
    if (!claimOwner()) {
      log("the builder is on, but another tab is driving it", "error");
      return;
    }
    state.running = true;
    state.nextAt = Date.now() + 5000;
    log(`▶️ resumed after a page load — ${session.count} built so far this run`);
    renderStatus();
  }

  function stop() {
    state.running = false;
    settings.enabled = false;
    saveSettings();
    releaseOwner();
    log("⏹️ stopped", "ok");
    renderStatus();
  }

  // ------------------------------------------------------------ self-test
  //
  // Everything this script cannot know from the outside — which types the
  // game offers, what the build buttons are called, where the purchase
  // links live — is read from the live pages here and printed. Read-only:
  // nothing is posted. Copy the result if something needs fixing.

  let lastDiagnostics = "";

  async function selfTest() {
    const lines = [`FRA Auto-Build ${VERSION} self-test — ${new Date().toISOString()}`];
    try {
      const doc = await fetchDoc("/buildings/new");
      lines.push("logged in: yes");
      const form = buildForm(doc);
      lines.push(`build form: ${form
        ? `#new_building → ${form.getAttribute("action")}` : "NOT FOUND"}`);
      const options = readTypeOptions(doc);
      lines.push(`building types offered: ${options.length}`);
      for (const option of options) {
        const button = personalButton(doc, option.value);
        lines.push(`  ${option.value} = ${option.label} — ` +
          (button.ok ? button.label : `NOT buildable: ${button.reason}`));
      }
      if (options.length) {
        cachedTypes = options;
        writeJson(TYPES_KEY, cachedTypes);
        renderTypes();
      }
      const fire = options.find((option) => /fire station/i.test(option.label));
      if (fire && settings.startingVehicle) {
        const vehicle = pickStartingVehicle(doc, fire.value, settings.startingVehicle);
        lines.push(`starting vehicle "${settings.startingVehicle}" on ${fire.label}: ` +
          (vehicle.applied ? `ok (${vehicle.field} = ${vehicle.value})`
            : `${vehicle.reason}${vehicle.offered ? ` — offered: ${vehicle.offered}` : ""}`));
      }
      const dispatch = buildDispatchSelect(doc);
      lines.push(`dispatch list on the build form: ${dispatch
        ? `${dispatch.options.length} options` : "NOT FOUND"}`);
      const anchor = settings.areaLat !== null
        ? [settings.areaLat, settings.areaLng]
        : [WORLD_POINTS[0][0], WORLD_POINTS[0][1]];
      const address = await gameAddress(anchor[0], anchor[1]);
      lines.push(`address from MissionChief: ${address || "(empty)"}`);
      if (!address) {
        const osm = await osmAddress(anchor[0], anchor[1]);
        lines.push(`address from OpenStreetMap: ${osm || "(none)"}`);
      }
    } catch (error) {
      lines.push(`build form: FAILED — ${error.message}`);
    }
    let probeId = null;
    try {
      const rows = await ownBuildings(true);
      lines.push(`your buildings (API): ${rows.length}`);
      const centers = await dispatchCenters();
      lines.push(`dispatch centers: ${centers.length}`);
      for (const center of centers.slice(0, 40)) lines.push(`  #${center.id} ${center.name}`);
      const rules = parseDispatchRules(settings.dispatchRules);
      lines.push(`dispatch rules configured: ${rules.length}`);
      const ids = new Set(centers.map((center) => center.id));
      const others = rows.filter((row) => !ids.has(String(row.id)));
      // Prefer a fire station that still has something to buy: it has a
      // level, storage rooms and a staff target, so every probe applies.
      const fireStations = others.filter((row) => parseInt(row.building_type, 10) === 0);
      const lowest = (fireStations.length ? fireStations : others).slice()
        .sort((a, b) => (a.level || 0) - (b.level || 0))[0];
      probeId = lowest ? lowest.id : (rows[0] ? rows[0].id : null);
    } catch (error) {
      lines.push(`/api/buildings: FAILED — ${error.message}`);
    }
    if (probeId) {
      // What the delivery step would find on a real building of yours.
      try {
        lines.push(`probe building #${probeId}:`);
        const page = await fetchDoc(`/buildings/${probeId}`);
        for (const [kind, hrefRe] of [["storage", STORAGE_HREF_RE],
                                      ["extension", EXTENSION_HREF_RE]]) {
          const links = purchaseLinks(page, probeId, hrefRe);
          const open = links.filter((link) => !link.disabled);
          lines.push(`  ${kind} (credits): ${open.length} buyable, ` +
            `${links.length - open.length} locked` +
            (open[0] ? ` — next: ${open[0].label} ${open[0].href.replace(/\?.*/, "")}` : ""));
        }
        const expand = await fetchDoc(`/buildings/${probeId}/expand`);
        const levels = purchaseLinks(expand, probeId, LEVEL_HREF_RE)
          .sort((a, b) => levelOf(b.href) - levelOf(a.href));
        lines.push(`  level (credits): ${levels.length} options` +
          (levels[0] ? ` — max: level ${levelOf(levels[0].href) + 1} for ${levels[0].label}` : ""));
        const edit = await fetchDoc(`/buildings/${probeId}/edit`);
        const form = editForm(edit, probeId);
        if (!form) {
          lines.push("  edit page: NO FORM");
        } else {
          const staff = staffFieldOnEdit(form);
          lines.push(`  staff field: ${staff
            ? `${staff.name || staff.id || "(unnamed)"} = ${staff.value}` : "NOT FOUND"}`);
          const select = dispatchSelect(form);
          lines.push(`  dispatch select: ${select
            ? `${select.name} (${select.options.length} options)` : "NOT FOUND"}`);
        }
      } catch (error) {
        lines.push(`probe building #${probeId}: FAILED — ${error.message}`);
      }
    }
    const credits = await readCredits();
    lines.push(`credits: ${credits === null ? "UNREADABLE (floor not enforced)"
      : credits.toLocaleString()}`);
    lastDiagnostics = lines.join("\n");
    for (const line of lines) log(line);
    log("📋 self-test done — use 'Copy report' to paste it somewhere", "ok");
  }

  // ------------------------------------------------------------------ UI

  const STYLE = `
    #fra-ab { position: fixed; right: 12px; bottom: 12px; width: 330px; z-index: 2147483600;
      font: 12px/1.45 system-ui, sans-serif; color: #eee; background: #1f2226;
      border: 1px solid #c0392b; border-radius: 8px; box-shadow: 0 6px 24px rgba(0,0,0,.45); }
    #fra-ab header { display: flex; align-items: center; justify-content: space-between;
      padding: 7px 10px; background: #c0392b; border-radius: 7px 7px 0 0; font-weight: 700; }
    #fra-ab header button { background: transparent; border: 0; color: #fff; cursor: pointer;
      font-size: 14px; padding: 0 4px; }
    #fra-ab .body { padding: 8px 10px 10px; max-height: 70vh; overflow: auto; }
    #fra-ab .row { display: flex; gap: 6px; align-items: center; margin: 4px 0; }
    #fra-ab .row > label { flex: 0 0 108px; color: #b9c0c8; }
    #fra-ab textarea { width: 100%; height: 52px; background: #2b3036; color: #eee;
      border: 1px solid #414852; border-radius: 4px; padding: 3px 5px;
      font: 11px/1.4 ui-monospace, monospace; resize: vertical; }
    #fra-ab .needs { margin-top: 6px; padding: 5px 6px; border-radius: 4px;
      background: #3a2c17; color: #ffd79a; cursor: pointer; }
    #fra-ab input[type=text], #fra-ab input[type=number], #fra-ab select {
      flex: 1 1 auto; min-width: 0; background: #2b3036; color: #eee;
      border: 1px solid #414852; border-radius: 4px; padding: 3px 5px; font: inherit; }
    #fra-ab .types { max-height: 150px; overflow: auto; background: #171a1d;
      border-radius: 4px; padding: 4px 6px; margin: 2px 0 4px; }
    #fra-ab .types label { display: flex; gap: 6px; align-items: center; color: #cfd6dd;
      padding: 1px 0; cursor: pointer; }
    #fra-ab .types .muted { color: #8c949c; padding: 4px 0; }
    #fra-ab .typebar { display: flex; gap: 6px; align-items: center; margin-top: 2px; }
    #fra-ab .typebar span { flex: 1 1 auto; color: #b9c0c8; }
    #fra-ab .typebar button { background: #313841; color: #eee; border: 1px solid #4a525c;
      border-radius: 4px; padding: 2px 7px; cursor: pointer; font: inherit; }
    #fra-ab .checks { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 8px; margin: 6px 0; }
    #fra-ab .checks label { display: flex; gap: 5px; align-items: center; color: #cfd6dd; }
    #fra-ab .buttons { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 6px; }
    #fra-ab .buttons button { flex: 1 1 auto; background: #313841; color: #eee; border: 1px solid #4a525c;
      border-radius: 4px; padding: 5px 8px; cursor: pointer; font: inherit; }
    #fra-ab .buttons button.go { background: #1e7d34; border-color: #29a145; font-weight: 700; }
    #fra-ab .buttons button.stop { background: #a02c22; border-color: #c0392b; font-weight: 700; }
    #fra-ab .status { padding: 5px 6px; background: #171a1d; border-radius: 4px; color: #9fd3ae; }
    #fra-ab .status b { color: #fff; }
    #fra-ab .log { margin-top: 6px; height: 150px; overflow: auto; background: #15181b;
      border-radius: 4px; padding: 5px 6px; font: 11px/1.4 ui-monospace, monospace; }
    #fra-ab .log div { white-space: pre-wrap; word-break: break-word; }
    #fra-ab .log .error { color: #ff9a8f; }
    #fra-ab .log .ok { color: #8fe6a4; }
    #fra-ab .log .info { color: #c6cdd4; }
    #fra-ab .note { color: #8c949c; margin-top: 6px; }
  `;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of children || []) node.appendChild(child);
    return node;
  }

  function labelled(text, input) {
    return el("div", { class: "row" }, [el("label", { text: text }), input]);
  }

  function checkbox(key, text) {
    const input = el("input", { type: "checkbox" });
    input.checked = !!settings[key];
    input.addEventListener("change", () => {
      settings[key] = input.checked;
      saveSettings();
      renderStatus();
    });
    return el("label", {}, [input, el("span", { text: text })]);
  }

  function numberField(key, min) {
    const input = el("input", { type: "number", min: String(min) });
    input.value = String(settings[key]);
    input.addEventListener("change", () => {
      const value = parseFloat(input.value);
      settings[key] = isFinite(value) ? value : DEFAULTS[key];
      input.value = String(settings[key]);
      saveSettings();
    });
    return input;
  }

  let panel = null;
  let menuItem = null;
  let needsBox = null;
  let startButtonPainter = null;
  let typeBox = null;
  let typeCount = null;
  let typesLoading = false;
  let statusBox = null;
  let logBox = null;

  function renderTypes() {
    if (!typeBox) return;
    typeBox.innerHTML = "";
    const types = knownTypes();
    if (!types.length) {
      typeBox.appendChild(el("div", { class: "muted", text: typesLoading
        ? "reading the game's building list…"
        : "no list yet — press Reload" }));
      if (typeCount) typeCount.textContent = "";
      return;
    }
    const wanted = new Set((settings.typeValues || []).map(String));
    for (const type of types) {
      const input = el("input", { type: "checkbox" });
      input.checked = wanted.has(String(type.value));
      input.addEventListener("change", () => {
        const set = new Set((settings.typeValues || []).map(String));
        if (input.checked) set.add(String(type.value));
        else set.delete(String(type.value));
        settings.typeValues = [...set];
        saveSettings();
        if (typeCount) {
          typeCount.textContent = `${settings.typeValues.length} of ${types.length} ticked`;
        }
      });
      typeBox.appendChild(el("label", { title: `type ${type.value}` },
        [input, el("span", { text: type.label })]));
    }
    if (typeCount) {
      typeCount.textContent =
        `${selectedTypes().length} of ${types.length} ticked`;
    }
  }

  async function loadTypes(announce) {
    // The list comes from the game's own build form, so it needs no id
    // table here and survives the game adding a building type. Read once
    // and cached; "Reload" re-reads it.
    if (typesLoading || state.busy) return;
    typesLoading = true;
    state.busy = true;
    renderTypes();
    try {
      const doc = await fetchDoc("/buildings/new");
      if (isLoginPage(doc)) {
        log("log in to MissionChief first, then press Reload", "error");
        return;
      }
      const options = readTypeOptions(doc);
      if (!options.length) {
        log("the build form offered no building types", "error");
        return;
      }
      cachedTypes = options;
      writeJson(TYPES_KEY, cachedTypes);
      if (announce) {
        log(`📋 ${options.length} building types read from the game`, "ok");
      }
    } catch (error) {
      log(`could not read the building list — ${error.message}`, "error");
    } finally {
      typesLoading = false;
      state.busy = false;
      renderTypes();
    }
  }

  function renderNeeds() {
    if (!needsBox) return;
    const entries = Object.entries(needsDispatch)
      .sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      needsBox.style.display = "none";
      return;
    }
    needsBox.style.display = "block";
    needsBox.textContent = "Needs a dispatch center: " +
      entries.map(([country, count]) => `${country} (${count})`).join(", ");
  }

  function renderStatus() {
    if (startButtonPainter) startButtonPainter();
    paintMenuItem();
    if (!statusBox) return;
    const parts = [
      state.running ? "<b>RUNNING</b>" : "idle",
      settings.dryRun ? "DRY RUN" : "<b>LIVE</b>",
      `built ${session.count}/${settings.maxPerSession}`,
      `finishing ${Object.keys(queue).length}`,
    ];
    if (state.running && state.nextAt > Date.now()) {
      parts.push(`next in ${Math.ceil((state.nextAt - Date.now()) / 1000)}s`);
    }
    if (state.credits !== null && state.credits !== undefined) {
      parts.push(`credits ${state.credits.toLocaleString()}`);
    }
    statusBox.innerHTML = parts.join(" · ");
  }

  function renderLog() {
    if (!logBox) return;
    const atBottom = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 20;
    logBox.innerHTML = "";
    for (const line of state.logLines.slice(-120)) {
      logBox.appendChild(el("div", { class: line.kind, text: `${line.at} ${line.text}` }));
    }
    if (atBottom) logBox.scrollTop = logBox.scrollHeight;
  }

  function buildPanel() {
    document.head.appendChild(el("style", { text: STYLE }));
    const body = el("div", { class: "body" });

    typeBox = el("div", { class: "types" });
    typeCount = el("span", {});
    const allButton = el("button", { text: "All" });
    allButton.addEventListener("click", () => {
      settings.typeValues = knownTypes().map((type) => String(type.value));
      saveSettings();
      renderTypes();
    });
    const noneButton = el("button", { text: "None" });
    noneButton.addEventListener("click", () => {
      settings.typeValues = [];
      saveSettings();
      renderTypes();
    });
    const reloadButton = el("button", { text: "Reload" });
    reloadButton.addEventListener("click", () => loadTypes(true));
    body.appendChild(el("div", { class: "row" }, [el("label", { text: "Buildings" })]));
    body.appendChild(typeBox);
    body.appendChild(el("div", { class: "typebar" },
      [typeCount, allButton, noneButton, reloadButton]));

    const modeSelect = el("select", {});
    for (const [value, text] of [
      ["world", "Random worldwide"],
      ["area", "Around a place"],
      ["mix", "Both (50/50)"],
    ]) modeSelect.appendChild(el("option", { value: value, text: text }));
    modeSelect.value = settings.locationMode;
    modeSelect.addEventListener("change", () => {
      settings.locationMode = modeSelect.value;
      saveSettings();
    });
    body.appendChild(labelled("Where", modeSelect));

    const areaInput = el("input", {
      type: "text", placeholder: "address or 51.92, 4.48",
    });
    areaInput.value = settings.areaText || "";
    areaInput.addEventListener("change", async () => {
      const text = areaInput.value.trim();
      settings.areaText = text;
      settings.areaLat = null;
      settings.areaLng = null;
      saveSettings();
      if (!text) return;
      const coords = parseCoords(text);
      if (coords) {
        settings.areaLat = coords[0];
        settings.areaLng = coords[1];
        saveSettings();
        log(`📌 area set to ${coords[0].toFixed(4)}, ${coords[1].toFixed(4)}`, "ok");
        return;
      }
      try {
        const [lat, lng, name] = await osmGeocode(text);
        settings.areaLat = lat;
        settings.areaLng = lng;
        saveSettings();
        log(`📌 area set to ${name} (${lat.toFixed(4)}, ${lng.toFixed(4)})`, "ok");
      } catch (error) {
        log(`❌ could not find "${text}": ${error.message}`, "error");
      }
    });
    body.appendChild(labelled("Place", areaInput));
    body.appendChild(labelled("Radius (km)", numberField("areaRadiusKm", 0.5)));
    body.appendChild(labelled("World spread (km)", numberField("jitterKm", 0.5)));
    body.appendChild(labelled("Every (seconds)", numberField("intervalSeconds", 20)));
    body.appendChild(labelled("Max this run", numberField("maxPerSession", 1)));
    body.appendChild(labelled("Credit floor", numberField("creditsFloor", 0)));

    const vehicleInput = el("input", { type: "text", placeholder: "Quint" });
    vehicleInput.value = settings.startingVehicle || "";
    vehicleInput.addEventListener("change", () => {
      settings.startingVehicle = vehicleInput.value.trim();
      saveSettings();
    });
    body.appendChild(labelled("Fire start car", vehicleInput));
    body.appendChild(labelled("Staff limit", numberField("staffLimit", 0)));
    body.appendChild(labelled("Max storage buys", numberField("maxStorageBuys", 0)));

    const rulesInput = el("textarea", {
      placeholder: "Dispatch rules, one per line:\nNetherlands = Rotterdam Dispatch\nTexas = Houston Dispatch",
    });
    rulesInput.value = settings.dispatchRules || "";
    rulesInput.addEventListener("change", () => {
      settings.dispatchRules = rulesInput.value;
      saveSettings();
      const rules = parseDispatchRules(settings.dispatchRules);
      log(`📖 ${rules.length} dispatch rule(s) loaded`, "ok");
    });
    body.appendChild(rulesInput);

    body.appendChild(el("div", { class: "checks" }, [
      checkbox("dryRun", "Dry run"),
      checkbox("verifyWithOsm", "Check address"),
      checkbox("linkDispatch", "Link dispatch"),
      checkbox("maxLevel", "Max level"),
      checkbox("buyStorage", "Buy storage"),
      checkbox("setStaffLimit", "Set staff limit"),
      checkbox("buyExtensions", "Extensions too"),
      checkbox("strictVehicle", "Quint required"),
    ]));

    const startButton = el("button", { class: "go", text: "Start" });
    const paintStartButton = () => {
      startButton.textContent = state.running ? "Stop" : "Start";
      startButton.className = state.running ? "stop" : "go";
    };
    startButton.addEventListener("click", () => {
      if (state.running) { stop(); } else { start(); }
      paintStartButton();
    });
    startButtonPainter = paintStartButton;
    const onceButton = el("button", { text: "Build one" });
    onceButton.addEventListener("click", async () => {
      if (state.busy) return log("busy — one thing at a time", "error");
      state.busy = true;
      try {
        const result = await buildOne();
        if (result.buildingId) await afterBuild(result);
      } catch (error) {
        log(`❌ ${error.message}`, "error");
      } finally {
        state.busy = false;
        renderStatus();
      }
    });
    const testButton = el("button", { text: "Self-test" });
    testButton.addEventListener("click", async () => {
      if (state.busy) return log("busy — one thing at a time", "error");
      state.busy = true;
      try { await selfTest(); } finally { state.busy = false; }
    });
    const copyButton = el("button", { text: "Copy report" });
    copyButton.addEventListener("click", async () => {
      const text = lastDiagnostics
        || state.logLines.map((line) => `${line.at} ${line.text}`).join("\n");
      try {
        await navigator.clipboard.writeText(text);
        log("📋 copied to the clipboard", "ok");
      } catch (error) {
        window.prompt("Copy this:", text.slice(0, 4000));
      }
    });
    const clearButton = el("button", { text: "Clear" });
    clearButton.addEventListener("click", () => {
      state.logLines = [];
      renderLog();
    });

    body.appendChild(el("div", { class: "buttons" },
      [startButton, onceButton, testButton, copyButton, clearButton]));

    statusBox = el("div", { class: "status" });
    body.appendChild(statusBox);
    needsBox = el("div", { class: "needs", title: "click to clear this list" });
    needsBox.addEventListener("click", () => {
      needsDispatch = {};
      saveNeeds();
      renderNeeds();
    });
    body.appendChild(needsBox);
    logBox = el("div", { class: "log" });
    body.appendChild(logBox);
    body.appendChild(el("div", { class: "note", text:
      "Personal builds with your own credits. Never spends coins. Dry run " +
      "does everything except the last click." }));

    const collapse = el("button", { text: "–", title: "collapse" });
    collapse.addEventListener("click", () => {
      body.style.display = body.style.display === "none" ? "block" : "none";
      collapse.textContent = body.style.display === "none" ? "+" : "–";
    });
    const close = el("button", { text: "×", title: "hide (reopen via the FRA menu)" });
    close.addEventListener("click", () => setPanelOpen(false));
    panel = el("div", { id: "fra-ab" }, [
      el("header", {}, [
        el("span", { text: `FRA Auto-Build ${VERSION}` }),
        el("span", {}, [collapse, close]),
      ]),
      body,
    ]);
    document.body.appendChild(panel);
    setPanelOpen(readJson(PANEL_OPEN_KEY, false) === true);
    renderTypes();
    renderNeeds();
    renderStatus();
    renderLog();
  }

  // ------------------------------------------------ FRA menu (shared block)
  // Every FRA userscript puts its entries under ONE "FRA" dropdown in the
  // game's navbar instead of each adding its own buttons. A userscript
  // cannot import another, so this block is copied VERBATIM into each
  // script; tests/test_userscript_menu.py fails when the copies drift.
  // Whichever script runs first builds the menu; the others find it by id
  // and slot their items in by `order` (public scripts < 50 <= private).
  function fraMenuList() {
    let list = document.getElementById("fra-menu-list");
    if (list) return list;
    if (!document.getElementById("fra-menu-style")) {
      const style = document.createElement("style");
      style.id = "fra-menu-style";
      style.textContent =
        "#fra-menu > a { font-weight: 700; }" +
        "#fra-menu-list { display: none; min-width: 220px; }" +
        "#fra-menu.open > #fra-menu-list { display: block; }" +
        "#fra-menu-list .fra-menu-head { padding: 3px 20px; font-size: 11px;" +
        " color: #999; text-transform: uppercase; }" +
        "#fra-menu-float { position: fixed; top: 8px; right: 8px; z-index: 2147483601;" +
        " list-style: none; margin: 0; padding: 0; }" +
        "#fra-menu-float #fra-menu > a { display: inline-block; padding: 6px 12px;" +
        " background: #c0392b; color: #fff; border-radius: 4px; text-decoration: none; }" +
        "#fra-menu-float #fra-menu-list { position: absolute; right: 0; left: auto;" +
        " background: #fff; list-style: none; padding: 5px 0; margin: 2px 0 0;" +
        " border: 1px solid rgba(0,0,0,.15); border-radius: 4px; }" +
        "#fra-menu-float #fra-menu-list a { display: block; padding: 3px 20px;" +
        " color: #333; white-space: nowrap; text-decoration: none; }";
      document.head.appendChild(style);
    }
    const root = document.createElement("li");
    root.id = "fra-menu";
    root.className = "dropdown";
    const toggle = document.createElement("a");
    toggle.href = "#";
    toggle.className = "dropdown-toggle";
    toggle.title = "Fire & Rescue Academy tools";
    toggle.append("FRA ");
    const caret = document.createElement("b");
    caret.className = "caret";
    toggle.appendChild(caret);
    list = document.createElement("ul");
    list.id = "fra-menu-list";
    list.className = "dropdown-menu";
    root.append(toggle, list);
    // Our own toggle, not Bootstrap's data-toggle: the game's Bootstrap
    // would otherwise toggle it a second time and cancel ours out.
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      root.classList.toggle("open");
    });
    document.addEventListener("click", (event) => {
      if (!root.contains(event.target)) root.classList.remove("open");
    });
    // In order of preference — one selector LIST would return whichever
    // match comes first in the page, i.e. the left-hand nav.
    let nav = null;
    for (const selector of [
      "#navbar-main-collapse ul.navbar-right",
      "#navbar-main-collapse ul.nav",
      ".navbar ul.nav",
    ]) {
      nav = document.querySelector(selector);
      if (nav) break;
    }
    if (nav) {
      nav.insertBefore(root, nav.firstChild);
    } else {
      const float = document.createElement("ul");
      float.id = "fra-menu-float";
      float.appendChild(root);
      document.body.appendChild(float);
    }
    return list;
  }

  function fraMenuClose() {
    const root = document.getElementById("fra-menu");
    if (root) root.classList.remove("open");
  }

  // item: { id, label, order, title?, onClick, header? } — `header` adds a
  // small section caption above the first item of that section.
  function fraMenuAdd(item) {
    const existing = document.getElementById(item.id);
    if (existing) return existing;
    const list = fraMenuList();
    const order = Number(item.order || 50);
    const li = document.createElement("li");
    li.id = item.id;
    li.dataset.fraOrder = String(order);
    const link = document.createElement("a");
    link.href = "#";
    link.textContent = item.label;
    if (item.title) link.title = item.title;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      fraMenuClose();
      item.onClick();
    });
    li.appendChild(link);
    const nodes = [li];
    if (item.header && !document.getElementById(item.id + "-head")) {
      const head = document.createElement("li");
      head.id = item.id + "-head";
      head.className = "fra-menu-head";
      head.dataset.fraOrder = String(order - 0.5);
      head.textContent = item.header;
      nodes.unshift(head);
    }
    for (const node of nodes) {
      const mine = Number(node.dataset.fraOrder);
      const after = Array.from(list.children)
        .find((other) => Number(other.dataset.fraOrder) > mine);
      list.insertBefore(node, after || null);
    }
    return li;
  }
  // ----------------------------------------------- /FRA menu (shared block)

  // The panel lives behind the shared FRA menu: hidden until opened there,
  // and the menu entry shows when the builder is running so a hidden panel
  // never hides a running builder.
  function setPanelOpen(open) {
    if (panel) panel.style.display = open ? "" : "none";
    writeJson(PANEL_OPEN_KEY, open);
  }

  function paintMenuItem() {
    if (!menuItem) return;
    menuItem.firstChild.textContent =
      "🏗️ Auto-Build" + (state.running ? (settings.dryRun ? " ● dry run" : " ● LIVE") : "");
  }

  function addMenuItem() {
    menuItem = fraMenuAdd({
      id: "fra-autobuild-menu",
      label: "🏗️ Auto-Build",
      order: 50,
      header: "Admin (private)",
      title: "Show/hide the FRA Auto-Build panel",
      onClick: () => setPanelOpen(!panel || panel.style.display === "none"),
    });
    paintMenuItem();
  }

  // ---------------------------------------------------------------- boot

  async function idleFinish() {
    // Extensions unlock as construction finishes, so the finish list has to
    // be worked even when the builder itself is off — otherwise a building
    // stays half delivered until the next run.
    if (state.busy || state.running) return;
    if (!Object.keys(queue).length) return;
    if (Date.now() < state.nextFinishAt) return;
    if (!claimOwner()) return;
    state.busy = true;
    try {
      await finishPass();
    } catch (error) {
      log(`⚠️ finish list: ${error.message}`, "error");
    } finally {
      state.nextFinishAt = Date.now() + 60000;
      state.busy = false;
      releaseOwner();      // idling must not keep another tab from starting
      renderStatus();
    }
  }

  function boot() {
    if (document.getElementById("fra-ab")) return;
    buildPanel();
    addMenuItem();
    log(`ready — ${cachedTypes.length} building types cached, ` +
        `${Object.keys(queue).length} on the finish list`);
    if (!cachedTypes.length) loadTypes(true);
    if (settings.enabled) resume();
    setInterval(() => {
      renderStatus();
      if (state.running) {
        claimOwner();
        tick();
      } else {
        idleFinish();
      }
    }, 3000);
    window.addEventListener("beforeunload", releaseOwner);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
