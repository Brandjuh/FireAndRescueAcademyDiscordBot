// ==UserScript==
// @name         FRA Auto-Build (private)
// @namespace    https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot
// @version      1.1.0
// @description  Private admin tool: bulk-build YOUR OWN MissionChief buildings. Pick a type, pick a place (fixed area and/or random worldwide), flip the toggle. Every new building is linked to the nearest dispatch center, fully expanded (all extensions/storage bought with credits) and fire stations start with a Quint.
// @match        https://www.missionchief.com/*
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
 * 1. picks a spot — random inside a radius around a place you name, random
 *    anywhere in the world, or alternating between the two;
 * 2. checks the spot has a real street address (the game's own pin lookup,
 *    with OpenStreetMap as a second opinion) so nothing lands in the sea;
 * 3. opens /buildings/new in a hidden frame, selects the type, drops the
 *    pin, and — for fire stations — picks the Quint as the free starting
 *    vehicle;
 * 4. submits with credits. NEVER with coins: build_with_coins stays 0 and
 *    any button whose label mentions coins is refused, twice over;
 * 5. links the finished building to the dispatch center that covers that
 *    REGION — matched by name against the country/region/city of the spot,
 *    with your own rule list on top ("Netherlands = Rotterdam Dispatch").
 *    A country where you have no center is never built in: the script
 *    skips it and puts the country on a list in the panel to ask you about;
 * 6. raises the LEVEL as far as it goes, sets the STAFF LIMIT, and buys
 *    every STORAGE slot. Extensions are OFF by default — switch them on
 *    yourself if you want them;
 * 7. keeps the building on a "finish" list, because extensions only unlock
 *    when the previous one finishes CONSTRUCTION — the list is revisited
 *    every few minutes until the building stops offering anything new.
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
 * Install in Tampermonkey, open missionchief.com, open the panel (bottom
 * right), press "Self-test". That reads the real build form and reports
 * what it found: the building types the game offers, whether the map pin
 * and address hooks are reachable, and your dispatch centers. Then pick a
 * type, set a location, leave DRY RUN on, and press Start once.
 */

(function () {
  "use strict";

  if (window.top !== window.self) return;   // never inside our own frames

  const VERSION = "1.1.0";
  const BASE = "https://www.missionchief.com";
  const SETTINGS_KEY = "fra_autobuild_settings";
  const QUEUE_KEY = "fra_autobuild_queue";
  const HISTORY_KEY = "fra_autobuild_history";
  const TYPES_KEY = "fra_autobuild_types";
  const SESSION_KEY = "fra_autobuild_session";
  const NEEDS_KEY = "fra_autobuild_needs_dispatch";
  const OWNER_KEY = "fra_autobuild_owner";
  const FRAME_ID = "fra-autobuild-frame";

  // One tab drives; the others idle. Same idea as the bot's job lock: a
  // heartbeat that goes stale when a tab is closed mid-run.
  const TAB_ID = Math.random().toString(36).slice(2);
  const OWNER_HEARTBEAT_MS = 5000;
  const OWNER_STALE_MS = 20000;

  const FRAME_TIMEOUT_MS = 45000;
  const ADDRESS_WAIT_MS = 12000;
  const MAX_LOCATION_TRIES = 12;     // per build: re-jitter, then re-pick
  const MAX_FINISH_PASSES = 12;      // purchase passes per building visit
  const FINISH_IDLE_LIMIT = 3;       // quiet visits before a building retires
  const FINISH_INTERVAL_MS = 5 * 60 * 1000;
  const DUPLICATE_RADIUS_M = 250;    // same figure the bot uses
  const NOMINATIM_REVERSE =
    "https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18";

  const DEFAULTS = {
    enabled: false,          // the toggle survives a page load
    dryRun: true,
    typeValue: "",
    typeLabel: "",
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
    linkDispatch: true,
    dispatchRules: "",               // "Netherlands = Dispatch Rotterdam" per line
    maxLevel: true,                  // raise the level as far as it goes
    setStaffLimit: true,
    staffLimit: 400,
    buyStorage: true,
    maxStorageBuys: 25,
    buyExtensions: false,            // OFF on purpose — see the header
    startingVehicle: "Quint",
    strictVehicle: true,
    showFrame: false,
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
  let session = readJson(SESSION_KEY, { count: 0, startedAt: 0 });
  let needsDispatch = readJson(NEEDS_KEY, {});   // country -> times skipped

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

  // ------------------------------------------------------------- the frame

  function ensureFrame() {
    let frame = document.getElementById(FRAME_ID);
    if (!frame) {
      frame = document.createElement("iframe");
      frame.id = FRAME_ID;
      document.body.appendChild(frame);
    }
    // NOT display:none — a hidden frame lays nothing out, and the build
    // form's own buttons are picked by "is it visible and does it have a
    // box". Parked off-screen it renders normally.
    frame.style.cssText = settings.showFrame
      ? "position:fixed;right:8px;bottom:360px;width:900px;height:620px;z-index:2147483000;" +
        "border:2px solid #c0392b;background:#fff;"
      : "position:fixed;left:-12000px;top:0;width:1280px;height:900px;border:0;" +
        "opacity:0.01;pointer-events:none;";
    return frame;
  }

  function frameGoto(path) {
    // Resolves with the frame's document once the navigation settles.
    const frame = ensureFrame();
    const url = path.startsWith("http") ? path : BASE + path;
    return new Promise((resolve, reject) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        frame.onload = null;
        reject(new Error("page did not load in time: " + path));
      }, FRAME_TIMEOUT_MS);
      frame.onload = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        frame.onload = null;
        // Give the page's own scripts a moment to wire the form up.
        setTimeout(() => {
          try {
            const doc = frame.contentDocument;
            if (!doc) {
              reject(new Error(
                "the frame is not readable — MissionChief may be blocking " +
                "frames (X-Frame-Options). Tell the bot author: this needs tab mode."
              ));
              return;
            }
            resolve({ frame, doc, win: frame.contentWindow });
          } catch (error) {
            reject(new Error("frame blocked: " + error.message));
          }
        }, 900);
      };
      try {
        frame.contentWindow.location.replace(url);
      } catch (error) {
        frame.src = url;
      }
    });
  }

  function isLoginPage(doc) {
    return !!doc.querySelector('input[type="password"]');
  }

  function flashText(doc) {
    const el = doc.querySelector(
      ".alert-danger, .alert.alert-error, #flash_error, .flash_error, " +
      "#error_explanation, .danger.alert"
    );
    if (!el) return "";
    return (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 300);
  }

  // -------------------------------------------------------- game adapters

  async function fetchDoc(path) {
    const response = await fetch(BASE + path, { credentials: "same-origin" });
    if (!response.ok) throw new Error(path + " -> HTTP " + response.status);
    const html = await response.text();
    return new DOMParser().parseFromString(html, "text/html");
  }

  async function fetchJson(path) {
    const response = await fetch(BASE + path, { credentials: "same-origin" });
    if (!response.ok) throw new Error(path + " -> HTTP " + response.status);
    return response.json();
  }

  function csrfToken(doc) {
    const meta = (doc || document).querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : null;
  }

  async function postPath(path, token) {
    // The purchase endpoints are Rails POSTs behind the CSRF token — the
    // same calls the bot makes server-side (extension/credits/<id>).
    const headers = { "X-Requested-With": "XMLHttpRequest" };
    if (token) headers["X-CSRF-Token"] = token;
    const response = await fetch(BASE + path, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
    });
    return response;
  }

  function readCredits() {
    // The navbar credit counter. Selector list is a best guess against the
    // live markup; when none of them match we simply don't enforce the
    // floor and say so once.
    const el = document.querySelector(
      "#credits_value, .credits-value, #navbar_credits, [id*='credits_value']"
    );
    if (!el) return null;
    const digits = (el.textContent || "").replace(/[^\d]/g, "");
    if (!digits) return null;
    return parseInt(digits, 10);
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
  // Everything below is a port of the bot's Playwright builder
  // (fra_bot/mc/browser_builder.py). Same order, same reasons: pick the
  // type so the page reveals its #detail_<id> block, MOVE THE MAP MARKER
  // (writing the hidden lat/lng alone leaves the address empty — the known
  // gotcha), then fill every field with real input/change events and click
  // a plain submit button rather than trusting a jQuery handler.

  function fieldByName(doc, win, name) {
    const escape = (win && win.CSS && win.CSS.escape)
      ? win.CSS.escape.bind(win.CSS)
      : (value) => value.replace(/["\\]/g, "\\$&");
    return doc.querySelector(`[name="${escape(name)}"]`);
  }

  function fieldValue(doc, win, name) {
    const field = fieldByName(doc, win, name);
    return field ? field.value || "" : "";
  }

  function dispatchEvents(field, win) {
    if (!field) return;
    for (const name of ["input", "change"]) {
      field.dispatchEvent(new win.Event(name, { bubbles: true }));
    }
  }

  function setField(doc, win, name, value) {
    const field = fieldByName(doc, win, name);
    if (!field) return false;
    field.value = String(value);
    dispatchEvents(field, win);
    return true;
  }

  function visibleText(element) {
    return [
      element && element.value,
      element && element.textContent,
      element && element.getAttribute && element.getAttribute("title"),
      element && element.getAttribute && element.getAttribute("aria-label"),
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  }

  function isVisible(element, win) {
    if (!element) return false;
    const style = win.getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (Number(style.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
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

  function selectType(doc, win, typeValue) {
    const select = doc.querySelector("#building_building_type")
      || doc.querySelector('[name="building[building_type]"]');
    if (!select) return { ok: false, error: "building type select not found" };
    select.value = String(typeValue);
    dispatchEvents(select, win);
    return select.value === String(typeValue)
      ? { ok: true }
      : { ok: false, error: `the game does not offer building type ${typeValue}` };
  }

  function setPosition(doc, win, cfg) {
    const nameEl = fieldByName(doc, win, "building[name]");
    if (nameEl) { nameEl.value = cfg.name; dispatchEvents(nameEl, win); }
    const coins = doc.querySelector("#build_with_coins");
    if (coins) coins.value = "0";
    const found = { marker: false, updateAddress: false };
    try {
      const marker = win.building_new_marker;
      if (marker) {
        found.marker = true;
        if (typeof marker.setLatLng === "function") {            // Leaflet
          marker.setLatLng([cfg.lat, cfg.lng]);
        } else if (win.mapkit) {                                  // Apple MapKit
          marker.coordinate = new win.mapkit.Coordinate(cfg.lat, cfg.lng);
        }
      }
      const latEl = doc.querySelector("#building_latitude");
      const lngEl = doc.querySelector("#building_longitude");
      if (latEl) latEl.value = cfg.lat;
      if (lngEl) lngEl.value = cfg.lng;
      if (typeof win.updateAddress === "function") {
        found.updateAddress = true;
        win.updateAddress();
      }
      return latEl && lngEl
        ? { ok: true, found }
        : { ok: false, error: "lat/lng fields not found", found };
    } catch (error) {
      return { ok: false, error: String(error), found };
    }
  }

  async function waitForAddress(doc) {
    const deadline = Date.now() + ADDRESS_WAIT_MS;
    while (Date.now() < deadline) {
      const el = doc.querySelector("#building_address");
      const value = el && el.value ? el.value.trim() : "";
      if (value) return value;
      await sleep(400);
    }
    const el = doc.querySelector("#building_address");
    return el && el.value ? el.value.trim() : "";
  }

  function startingVehicleSelects(doc, win) {
    // The free first vehicle of a new station. Which field it is (and
    // whether it exists at all) differs per building type, so this looks
    // for ANY visible select on the form that offers vehicle-looking
    // options, rather than hardcoding a field name we cannot verify.
    return [...doc.querySelectorAll("select")].filter((select) => {
      if (select.id === "building_building_type") return false;
      if (!isVisible(select, win)) return false;
      const name = (select.name || select.id || "").toLowerCase();
      if (name.includes("leitstelle") || name.includes("dispatch")) return false;
      return select.options.length > 0;
    });
  }

  function pickStartingVehicle(doc, win, wanted) {
    const needle = String(wanted || "").trim().toLowerCase();
    if (!needle) return { applied: false, reason: "no preference set" };
    const selects = startingVehicleSelects(doc, win);
    if (!selects.length) {
      return { applied: false, reason: "no starting-vehicle select on the form" };
    }
    for (const select of selects) {
      const match = [...select.options].find((option) =>
        (option.textContent || "").toLowerCase().includes(needle)
      );
      if (match) {
        select.value = match.value;
        dispatchEvents(select, win);
        return {
          applied: true,
          field: select.name || select.id || "(unnamed select)",
          label: (match.textContent || "").replace(/\s+/g, " ").trim(),
        };
      }
    }
    return {
      applied: false,
      reason: `"${wanted}" is not offered`,
      offered: selects
        .map((select) => [...select.options]
          .map((option) => (option.textContent || "").trim())
          .join(" | "))
        .join(" // ")
        .slice(0, 400),
    };
  }

  function allianceDepth(button, doc) {
    // How many levels up the "Build as Alliance Building" heading sits.
    // The bot only ever had to FIND the alliance button, so a plain
    // "does any ancestor mention it" was enough. This script also has to
    // find the PERSONAL button, and on a page where both live in one
    // container that test says "alliance" for both. Depth separates them:
    // the alliance button sits closest to its own heading.
    let depth = 1;
    for (let node = button && button.parentElement;
         node && node !== doc.body;
         node = node.parentElement, depth++) {
      const text = visibleText(node).toLowerCase();
      if (text.includes("build as alliance building")) return depth;
      if (node.matches && node.matches("form")) return Infinity;
    }
    return Infinity;
  }

  function pickBuildButton(doc, win, wantAlliance) {
    const buttons = [...doc.querySelectorAll(
      'input[type="submit"], button[type="submit"], button:not([type])'
    )];
    const described = buttons.map((button, index) => ({
      button,
      index,
      text: visibleText(button),
      depth: allianceDepth(button, doc),
      visible: isVisible(button, win),
      disabled: !!(button.disabled || button.hasAttribute("disabled")),
    }));
    let candidates = described.filter((item) => {
      const text = item.text.toLowerCase();
      return item.visible
        && !item.disabled
        && text.includes("build")
        && text.includes("credits")
        && !text.includes("coins");   // never, under any circumstance
    });
    if (wantAlliance) {
      candidates = candidates.filter((item) => item.depth !== Infinity);
      candidates.sort((a, b) => a.depth - b.depth);     // closest heading wins
    } else {
      candidates.sort((a, b) => b.depth - a.depth);     // furthest from it wins
      // A button whose own label says "alliance" is never the personal one.
      candidates = candidates.filter(
        (item) => !item.text.toLowerCase().includes("alliance"));
    }
    if (!candidates.length) {
      return {
        ok: false,
        reason: wantAlliance
          ? "no enabled alliance build button (credits) was found"
          : "no enabled personal build button (credits) was found",
        seen: described
          .filter((item) => item.text)
          .map((item) =>
            `${item.text}${item.depth !== Infinity ? " [alliance]" : ""}` +
            `${item.visible ? "" : " [hidden]"}${item.disabled ? " [disabled]" : ""}`)
          .slice(0, 12),
      };
    }
    return { ok: true, index: candidates[0].index, label: candidates[0].text };
  }

  function formSnapshot(doc, win) {
    return {
      type: fieldValue(doc, win, "building[building_type]"),
      name: fieldValue(doc, win, "building[name]"),
      lat: fieldValue(doc, win, "building[latitude]"),
      lng: fieldValue(doc, win, "building[longitude]"),
      address: fieldValue(doc, win, "building[address]"),
      alliance: fieldValue(doc, win, "build_as_alliance"),
      coins: fieldValue(doc, win, "build_with_coins"),
    };
  }

  function prepareForm(doc, win, cfg) {
    const form = doc.querySelector("#new_building")
      || doc.querySelector('form[action*="/buildings"]');
    if (!form) return { ok: false, reason: "the building form was not loaded" };
    if (!setField(doc, win, "building[name]", cfg.name)) {
      return { ok: false, reason: "the name field was not found" };
    }
    if (!setField(doc, win, "building[latitude]", cfg.lat)) {
      return { ok: false, reason: "the latitude field was not found" };
    }
    if (!setField(doc, win, "building[longitude]", cfg.lng)) {
      return { ok: false, reason: "the longitude field was not found" };
    }
    setField(doc, win, "building[address]", cfg.address || "");
    setField(doc, win, "build_with_coins", "0");
    setField(doc, win, "build_as_alliance", cfg.alliance ? "1" : "0");
    const buildAnother = fieldByName(doc, win, "build_another");
    if (buildAnother) {
      buildAnother.checked = false;
      dispatchEvents(buildAnother, win);
    }
    const button = pickBuildButton(doc, win, cfg.alliance);
    if (!button.ok) {
      return { ok: false, reason: button.reason, seen: button.seen,
               snapshot: formSnapshot(doc, win) };
    }
    return { ok: true, index: button.index, label: button.label,
             snapshot: formSnapshot(doc, win) };
  }

  function waitForFrameLoad(frame) {
    return new Promise((resolve, reject) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        frame.onload = null;
        reject(new Error("the build did not confirm in time"));
      }, FRAME_TIMEOUT_MS);
      frame.onload = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        frame.onload = null;
        setTimeout(() => resolve(frame.contentDocument), 800);
      };
    });
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

  async function pickLocation() {
    // Returns {lat, lng, label, address, place, dispatch} for a spot that is
    // not a duplicate, has a street address, and — when dispatch linking is
    // on — sits in a region one of your dispatch centers covers. A spot that
    // fails the last test is skipped and its country noted, not built.
    const centers = settings.linkDispatch ? await dispatchCenters() : [];
    const rules = parseDispatchRules(settings.dispatchRules);
    const needsPlace = settings.verifyWithOsm || settings.linkDispatch;
    let lastReason = "";
    for (let attempt = 0; attempt < MAX_LOCATION_TRIES; attempt++) {
      const anchor = pickAnchor();
      const [lat, lng] = offsetPoint(anchor.lat, anchor.lng, anchor.radiusKm);
      if (tooCloseToHistory(lat, lng)) {
        lastReason = "too close to something this script already built";
        continue;
      }
      if (!needsPlace) {
        return { lat, lng, label: anchor.label, address: null, place: null,
                 dispatch: null };
      }
      const place = await osmPlace(lat, lng);
      if (!place) {
        lastReason = "no street address there (water/desert)";
        await sleep(1100);   // Nominatim asks for at most one call per second
        continue;
      }
      if (!settings.linkDispatch) {
        return { lat, lng, label: anchor.label, address: place.address, place,
                 dispatch: null };
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
        dispatch: match.center, dispatchWhy: match.why,
      };
    }
    throw new Error(
      `could not find a usable spot in ${MAX_LOCATION_TRIES} tries — ${lastReason}`
    );
  }

  function buildingName(typeLabel, placeLabel) {
    const place = String(placeLabel || "").split(",")[0].trim();
    const name = `${place || "New"} ${typeLabel}`.replace(/\s+/g, " ").trim();
    return name.slice(0, 40);   // the game's name limit
  }

  // ---------------------------------------------------------- build a one

  async function buildOne() {
    const typeLabel = settings.typeLabel || "building";
    const spot = await pickLocation();
    log(`📍 ${typeLabel}: ${spot.lat.toFixed(5)}, ${spot.lng.toFixed(5)} ` +
        `near ${spot.label}` +
        (spot.dispatch ? ` → dispatch "${spot.dispatch.name}" (${spot.dispatchWhy})` : ""));

    const { frame, doc, win } = await frameGoto("/buildings/new");
    if (isLoginPage(doc)) throw new Error("you are logged out of MissionChief");

    const options = readTypeOptions(doc);
    if (options.length) {
      cachedTypes = options;
      writeJson(TYPES_KEY, cachedTypes);
    }
    const chosen = selectType(doc, win, settings.typeValue);
    if (!chosen.ok) throw new Error(chosen.error);
    await sleep(500);   // the page reveals the type's own block on 'change'

    const name = buildingName(typeLabel, spot.label);
    const placed = setPosition(doc, win, { name, lat: spot.lat, lng: spot.lng });
    if (!placed.ok) throw new Error(placed.error);

    let address = await waitForAddress(doc);
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

    const wantsVehicle = /fire station|feuerwache/i.test(typeLabel)
      ? settings.startingVehicle
      : "";
    let vehicle = { applied: false, reason: "not a fire station" };
    if (wantsVehicle) {
      vehicle = pickStartingVehicle(doc, win, wantsVehicle);
      if (!vehicle.applied && settings.strictVehicle) {
        const extra = vehicle.offered ? ` Offered: ${vehicle.offered}` : "";
        throw new Error(
          `STOP — the starting vehicle "${wantsVehicle}" could not be selected ` +
          `(${vehicle.reason}).${extra}`
        );
      }
      if (vehicle.applied) log(`🚒 starting vehicle: ${vehicle.label}`);
    }

    const prep = prepareForm(doc, win, {
      name,
      lat: spot.lat,
      lng: spot.lng,
      address,
      alliance: false,          // always a personal build, your own credits
    });
    if (!prep.ok) {
      const seen = prep.seen ? ` Buttons seen: ${prep.seen.join(" / ")}` : "";
      throw new Error(`${prep.reason}.${seen}`);
    }
    if (String(prep.snapshot.coins) !== "0") {
      throw new Error("refusing to build: build_with_coins is not 0");
    }

    if (settings.dryRun) {
      log(`🧪 DRY RUN — would click "${prep.label}" for "${name}" ` +
          `(address via ${addressFrom}: ${address})`, "ok");
      return { dryRun: true };
    }

    const landed = waitForFrameLoad(frame);
    const buttons = [...doc.querySelectorAll(
      'input[type="submit"], button[type="submit"], button:not([type])'
    )];
    const button = buttons[prep.index];
    if (!button) throw new Error("the build button vanished before the click");
    button.click();
    const after = await landed;

    let buildingId = null;
    const href = String(frame.contentWindow.location.href || "");
    const match = href.match(/\/(?:alliance_)?buildings\/(\d+)/);
    if (match) buildingId = parseInt(match[1], 10);
    if (!buildingId) {
      buildingId = await confirmByApi(spot.lat, spot.lng);
    }
    if (!buildingId) {
      const flash = after ? flashText(after) : "";
      throw new Error(
        "the build did not confirm — MissionChief did not return a building" +
        (flash ? `: ${flash}` : " (no error shown)")
      );
    }

    history.push({
      id: buildingId, lat: spot.lat, lng: spot.lng,
      type: typeLabel, at: new Date().toISOString(),
    });
    saveHistory();
    log(`✅ built #${buildingId} "${name}" near ${spot.label}`, "ok");
    return { buildingId, name, lat: spot.lat, lng: spot.lng, spot };
  }

  async function confirmByApi(lat, lng) {
    // The alliance/personal build does not always redirect to the new
    // building, so confirm the way the bot does: ask the API and look for
    // something of ours within a few dozen metres of the pin.
    try {
      const rows = await ownBuildings(true);
      let best = null;
      let bestDistance = 120;
      for (const row of rows) {
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

  function dispatchSelect(doc, win) {
    const direct = doc.querySelector(
      'select[name*="leitstelle"], select[id*="leitstelle"], ' +
      'select[name*="dispatch"], select[id*="dispatch"]'
    );
    if (direct) return direct;
    // Fall back to any select whose label text mentions a dispatch center.
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

  async function linkDispatch(buildingId, center) {
    if (!center) return { ok: false, reason: "no dispatch center was chosen" };
    for (const path of [`/buildings/${buildingId}`, `/buildings/${buildingId}/edit`]) {
      let page;
      try {
        page = await frameGoto(path);
      } catch (error) {
        continue;
      }
      const select = dispatchSelect(page.doc, page.win);
      if (!select) continue;
      const option = optionForCenter(select, center);
      if (!option) {
        return {
          ok: false,
          reason: `"${center.name}" is not in the dispatch list on the ` +
                  "building page (options: " +
                  [...select.options].map((entry) => visibleText(entry))
                    .join(" | ").slice(0, 200) + ")",
        };
      }
      if (String(select.value) === String(option.value)) {
        return { ok: true, already: true, label: center.name };
      }
      if (settings.dryRun) {
        return { ok: true, dryRun: true, label: center.name };
      }
      select.value = String(option.value);
      dispatchEvents(select, page.win);
      const form = select.closest("form");
      if (!form) return { ok: false, reason: "the dispatch select is not in a form" };
      const landed = waitForFrameLoad(page.frame);
      const submit = form.querySelector(
        'input[type="submit"], button[type="submit"]'
      );
      if (submit) submit.click();
      else if (form.requestSubmit) form.requestSubmit();
      else form.submit();
      await landed;
      return { ok: true, label: center.name };
    }
    return { ok: false, reason: "no dispatch center field on the building page" };
  }

  // --------------------------------------------------- deliver a building
  //
  // Deliberately NOT "buy everything the page offers": extensions are OFF
  // by default. A new building gets its LEVEL raised as far as it goes,
  // its STAFF LIMIT set, and every STORAGE slot bought — nothing else,
  // unless you switch extensions on yourself.
  //
  // All of it happens in the frame, on the real page, because these
  // controls are a mix of plain links, Rails POST links and small forms,
  // and clicking the real thing runs the page's own handlers. After every
  // click the building page is re-loaded and re-scanned: purchases unlock
  // one at a time, exactly like the bot's finisher.

  const STORAGE_RE = /storage|lager/i;
  const LEVEL_RE = /expand_do|expand\/|\/expand/i;
  const EXTENSION_RE = /\/extension\//i;
  const STAFF_NAME_RE = /personal|personnel|staff|crew|besetzung/i;
  const MAX_DELIVERY_STEPS = 40;

  function creditLinks(doc, buildingId) {
    const prefix = `/buildings/${buildingId}/`;
    const links = [];
    for (const anchor of doc.querySelectorAll("a[href], button, input[type='submit']")) {
      let href = anchor.getAttribute ? (anchor.getAttribute("href") || "") : "";
      if (href.startsWith(BASE)) href = href.slice(BASE.length);
      const label = visibleText(anchor).replace(/\s+/g, " ").trim();
      const haystack = `${href} ${label}`;
      if (/coin/i.test(haystack)) continue;              // never, ever
      const isLink = href.startsWith(prefix) && /credits/i.test(href);
      // The storage control is the one thing here that is NOT verified
      // against the live page: it may be a button, a link, or its own page.
      // So the match is deliberately wide, and the self-test prints what it
      // found on a real building of yours before any of it is clicked.
      const isStorage = STORAGE_RE.test(label)
        && (!href || href.startsWith(prefix));
      if (!isLink && !isStorage) continue;
      links.push({ element: anchor, href, label });
    }
    return links;
  }

  function categorize(link) {
    if (STORAGE_RE.test(link.href) || STORAGE_RE.test(link.label)) return "storage";
    if (LEVEL_RE.test(link.href)) return "level";
    if (EXTENSION_RE.test(link.href)) return "extension";
    return "other";
  }

  function wantedCategories() {
    const wanted = new Set();
    if (settings.maxLevel) wanted.add("level");
    if (settings.buyStorage) wanted.add("storage");
    if (settings.buyExtensions) wanted.add("extension");
    return wanted;
  }

  async function clickAndSettle(page, element, buildingId) {
    // Some of these controls navigate, some are AJAX. Wait for a load if
    // one comes, then re-open the building page either way.
    const landed = waitForFrameLoad(page.frame).catch(() => null);
    element.click();
    await Promise.race([landed, sleep(6000)]);
    await sleep(600);
    return frameGoto(`/buildings/${buildingId}`);
  }

  function staffField(doc, win) {
    const fields = [...doc.querySelectorAll(
      'input[type="number"], input[type="text"], select'
    )];
    for (const field of fields) {
      const name = (field.name || field.id || "").toLowerCase();
      if (STAFF_NAME_RE.test(name)) return field;
    }
    for (const field of fields) {
      const context = normalize(visibleText(
        field.closest("form, div, td, li, label") || field));
      if (/staff|personnel|crew/.test(context)
          && /max|limit|target|amount/.test(context)) {
        return field;
      }
    }
    return null;
  }

  async function applyStaffLimit(page) {
    const field = staffField(page.doc, page.win);
    if (!field) return { ok: false, reason: "no staff-limit field on the page" };
    const target = Math.max(0, parseInt(settings.staffLimit, 10) || 0);
    const max = parseInt(field.getAttribute("max") || "", 10);
    const value = isFinite(max) && max > 0 ? Math.min(target, max) : target;
    const current = parseInt(String(field.value).replace(/[^\d]/g, ""), 10);
    if (isFinite(current) && current >= value) {
      return { ok: true, already: true, value: current };
    }
    if (settings.dryRun) return { ok: true, dryRun: true, value: value };
    field.value = String(value);
    dispatchEvents(field, page.win);
    const form = field.closest("form");
    if (!form) return { ok: false, reason: "the staff field is not in a form" };
    const landed = waitForFrameLoad(page.frame).catch(() => null);
    const submit = form.querySelector('input[type="submit"], button[type="submit"]');
    if (submit) submit.click();
    else if (form.requestSubmit) form.requestSubmit();
    else form.submit();
    await Promise.race([landed, sleep(6000)]);
    return { ok: true, value: value, field: field.name || field.id || "(unnamed)" };
  }

  async function deliverBuilding(buildingId) {
    const counts = { level: 0, storage: 0, extension: 0 };
    const labels = [];
    let page = await frameGoto(`/buildings/${buildingId}`);

    if (settings.setStaffLimit) {
      const staff = await applyStaffLimit(page);
      if (staff.ok && !staff.already) {
        labels.push(`staff limit ${staff.value}${staff.dryRun ? " [dry run]" : ""}`);
      } else if (!staff.ok) {
        log(`⚠️ #${buildingId}: staff limit not set — ${staff.reason}`, "error");
      }
      page = await frameGoto(`/buildings/${buildingId}`);
    }

    const wanted = wantedCategories();
    const attempted = new Set();
    let storageBought = 0;
    for (let step = 0; step < MAX_DELIVERY_STEPS; step++) {
      if (!wanted.size) break;
      const links = creditLinks(page.doc, buildingId)
        .map((link) => Object.assign(link, { kind: categorize(link) }))
        .filter((link) => wanted.has(link.kind))
        .filter((link) => !attempted.has(link.href || link.label));
      if (!links.length) break;
      const next = links[0];
      const key = next.href || next.label;
      if (next.kind === "storage") {
        // The storage button stays on the page and buys ONE slot per press,
        // so it must not go on the "already tried" list — it is bounded by
        // its own cap instead. Everything else is a one-off offer.
        if (storageBought >= (parseInt(settings.maxStorageBuys, 10) || 0)) {
          attempted.add(key);
          continue;
        }
        storageBought += 1;
      } else {
        attempted.add(key);
      }
      counts[next.kind] += 1;
      labels.push(`${next.label || next.href}`.slice(0, 60));
      if (settings.dryRun) {
        if (next.kind === "storage") attempted.add(key);   // report it once
        continue;
      }
      page = await clickAndSettle(page, next.element, buildingId);
      await sleep(1000);
    }
    return { counts, labels: labels.slice(0, 12) };
  }

  // ------------------------------------------------------- the finish list

  function enqueue(buildingId, label) {
    queue[String(buildingId)] = {
      label: label || "",
      idle: 0,
      addedAt: Date.now(),
      nextAt: Date.now() + FINISH_INTERVAL_MS,
    };
    saveQueue();
  }

  async function finishPass() {
    const now = Date.now();
    for (const [key, entry] of Object.entries(queue)) {
      if ((entry.nextAt || 0) > now) continue;
      const buildingId = parseInt(key, 10);
      let result;
      try {
        result = await deliverBuilding(buildingId);
      } catch (error) {
        log(`⚠️ finish #${buildingId}: ${error.message}`, "error");
        entry.nextAt = now + FINISH_INTERVAL_MS;
        saveQueue();
        continue;
      }
      const bought = result.counts.level + result.counts.storage
        + result.counts.extension;
      if (bought > 0) {
        entry.idle = 0;
        log(`🏗️ #${buildingId}: ${bought} more ` +
            `(${result.labels.join(", ")})`);
      } else {
        entry.idle = (entry.idle || 0) + 1;
      }
      entry.nextAt = now + FINISH_INTERVAL_MS;
      if (entry.idle >= FINISH_IDLE_LIMIT) {
        delete queue[key];
        log(`🏁 #${buildingId} is fully delivered — off the finish list`, "ok");
      }
      saveQueue();
      return;   // one building per pass keeps the traffic gentle
    }
  }

  // --------------------------------------------------------------- the loop

  function creditsOk() {
    state.credits = readCredits();
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
      if (!settings.typeValue) {
        log("⏹️ pick a building type first — stopped", "error");
        stop();
        return;
      }
      if (!creditsOk()) {
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
      if (result.buildingId) {
        if (settings.linkDispatch) {
          try {
            const linked = await linkDispatch(
              result.buildingId, result.spot && result.spot.dispatch);
            if (linked.ok) {
              log(`🛰️ #${result.buildingId} → dispatch "${linked.label}"` +
                  (linked.dryRun ? " [dry run]" : linked.already ? " (already set)" : ""));
            } else {
              log(`⚠️ #${result.buildingId}: dispatch not linked — ${linked.reason}`,
                  "error");
            }
          } catch (error) {
            log(`⚠️ #${result.buildingId}: dispatch link failed — ${error.message}`,
                "error");
          }
        }
        const delivered = await deliverBuilding(result.buildingId);
        log(`🧱 #${result.buildingId}: ${settings.dryRun ? "would do" : "done"} — ` +
            `level ${delivered.counts.level}, storage ${delivered.counts.storage}` +
            (settings.buyExtensions ? `, extensions ${delivered.counts.extension}` : "") +
            (delivered.labels.length ? ` — ${delivered.labels.join(", ")}` : ""));
        enqueue(result.buildingId, result.name);
      }
    } catch (error) {
      state.lastError = error.message;
      log(`❌ ${error.message}`, "error");
      // A hard stop (a missing Quint, a logged-out session, a blocked
      // frame) must not be retried in a loop — those repeat forever.
      if (/^STOP —|logged out|frame/.test(error.message)) stop();
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
    session = { count: 0, startedAt: Date.now() };
    saveSession();
    state.nextAt = 0;
    log(`▶️ started — ${settings.dryRun ? "DRY RUN" : "LIVE"}, ` +
        `${settings.typeLabel || "no type!"}, mode ${settings.locationMode}`, "ok");
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
  // game offers, whether the map hooks exist, what the build buttons are
  // called — is read from the live page here and printed. Copy the result
  // if something needs fixing.

  let lastDiagnostics = "";

  async function selfTest() {
    const lines = [`FRA Auto-Build ${VERSION} self-test — ${new Date().toISOString()}`];
    try {
      const { doc, win } = await frameGoto("/buildings/new");
      lines.push(`logged in: ${isLoginPage(doc) ? "NO — log in first" : "yes"}`);
      const options = readTypeOptions(doc);
      lines.push(`building types offered: ${options.length}`);
      for (const option of options) lines.push(`  ${option.value} = ${option.label}`);
      if (options.length) {
        cachedTypes = options;
        writeJson(TYPES_KEY, cachedTypes);
        renderTypes();
      }
      const probe = settings.typeValue
        || (options.find((option) => /fire station/i.test(option.label)) || options[0] || {}).value;
      if (probe) {
        const chosen = selectType(doc, win, probe);
        lines.push(`select type ${probe}: ${chosen.ok ? "ok" : chosen.error}`);
        await sleep(600);
      }
      const anchor = settings.areaLat !== null
        ? [settings.areaLat, settings.areaLng]
        : [WORLD_POINTS[0][0], WORLD_POINTS[0][1]];
      const placed = setPosition(doc, win, {
        name: "FRA self-test", lat: anchor[0], lng: anchor[1],
      });
      lines.push(`map marker global found: ${placed.found && placed.found.marker}`);
      lines.push(`updateAddress() found: ${placed.found && placed.found.updateAddress}`);
      lines.push(`lat/lng fields: ${placed.ok ? "ok" : placed.error}`);
      const address = await waitForAddress(doc);
      lines.push(`address from MissionChief: ${address || "(empty)"}`);
      if (!address) {
        const osm = await osmAddress(anchor[0], anchor[1]);
        lines.push(`address from OpenStreetMap: ${osm || "(none)"}`);
      }
      const selects = startingVehicleSelects(doc, win);
      lines.push(`extra selects on the form: ${selects.length}`);
      for (const select of selects.slice(0, 4)) {
        lines.push(`  ${select.name || select.id || "(unnamed)"}: ` +
          [...select.options].slice(0, 25)
            .map((option) => (option.textContent || "").trim()).join(" | ")
            .slice(0, 300));
      }
      const personal = pickBuildButton(doc, win, false);
      const alliance = pickBuildButton(doc, win, true);
      lines.push(`personal build button: ${personal.ok ? personal.label : personal.reason}`);
      lines.push(`alliance build button: ${alliance.ok ? alliance.label : alliance.reason}`);
      if (!personal.ok && personal.seen) {
        lines.push(`  buttons seen: ${personal.seen.join(" / ")}`);
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
      for (const center of centers) lines.push(`  #${center.id} ${center.name}`);
      const rules = parseDispatchRules(settings.dispatchRules);
      lines.push(`dispatch rules configured: ${rules.length}`);
      const ids = new Set(centers.map((center) => center.id));
      const other = rows.find((row) => !ids.has(String(row.id)));
      probeId = other ? other.id : (rows[0] ? rows[0].id : null);
    } catch (error) {
      lines.push(`/api/buildings: FAILED — ${error.message}`);
    }
    if (probeId) {
      // What the delivery step would actually find on a real building of
      // yours: the staff field, the storage button, the level chain.
      try {
        const page = await frameGoto(`/buildings/${probeId}`);
        lines.push(`probe building #${probeId}:`);
        const staff = staffField(page.doc, page.win);
        lines.push(`  staff field: ${staff
          ? `${staff.name || staff.id || "(unnamed)"} = ${staff.value}` +
            (staff.getAttribute("max") ? ` (max ${staff.getAttribute("max")})` : "")
          : "NOT FOUND"}`);
        const links = creditLinks(page.doc, probeId)
          .map((link) => Object.assign(link, { kind: categorize(link) }));
        lines.push(`  credit controls: ${links.length}`);
        for (const link of links.slice(0, 15)) {
          lines.push(`    [${link.kind}] ${link.label || "(no label)"} ${link.href}`);
        }
        const select = dispatchSelect(page.doc, page.win);
        lines.push(`  dispatch select: ${select
          ? [...select.options].map((option) => visibleText(option))
              .join(" | ").slice(0, 200)
          : "NOT FOUND on the building page"}`);
      } catch (error) {
        lines.push(`probe building #${probeId}: FAILED — ${error.message}`);
      }
    }
    lines.push(`credits read from the page: ${readCredits()}`);
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
      if (key === "showFrame") ensureFrame();
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
  let needsBox = null;
  let startButtonPainter = null;
  let typeSelect = null;
  let statusBox = null;
  let logBox = null;

  function renderTypes() {
    if (!typeSelect) return;
    typeSelect.innerHTML = "";
    if (!cachedTypes.length) {
      typeSelect.appendChild(el("option", { value: "", text: "— run Self-test first —" }));
      return;
    }
    typeSelect.appendChild(el("option", { value: "", text: "— pick a type —" }));
    for (const type of cachedTypes) {
      const option = el("option", { value: type.value, text: type.label });
      typeSelect.appendChild(option);
    }
    typeSelect.value = settings.typeValue || "";
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

    typeSelect = el("select", {});
    typeSelect.addEventListener("change", () => {
      settings.typeValue = typeSelect.value;
      settings.typeLabel = typeSelect.selectedOptions.length
        ? typeSelect.selectedOptions[0].textContent : "";
      saveSettings();
    });
    body.appendChild(labelled("Building", typeSelect));

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
      checkbox("showFrame", "Show its work"),
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
        if (result.buildingId) {
          if (settings.linkDispatch) {
            const linked = await linkDispatch(
              result.buildingId, result.spot && result.spot.dispatch);
            log(linked.ok
              ? `🛰️ dispatch "${linked.label}"${linked.dryRun ? " [dry run]" : ""}`
              : `⚠️ dispatch not linked — ${linked.reason}`, linked.ok ? "info" : "error");
          }
          const delivered = await deliverBuilding(result.buildingId);
          log(`🧱 ${settings.dryRun ? "would do" : "done"} — level ` +
              `${delivered.counts.level}, storage ${delivered.counts.storage}`);
          enqueue(result.buildingId, result.name);
        }
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
    panel = el("div", { id: "fra-ab" }, [
      el("header", {}, [el("span", { text: `FRA Auto-Build ${VERSION}` }), collapse]),
      body,
    ]);
    document.body.appendChild(panel);
    renderTypes();
    renderNeeds();
    renderStatus();
    renderLog();
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
    log(`ready — ${cachedTypes.length} building types cached, ` +
        `${Object.keys(queue).length} on the finish list`);
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
