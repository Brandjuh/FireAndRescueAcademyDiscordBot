// ==UserScript==
// @name         FRA Dispatch Builder (private)
// @namespace    https://github.com/Brandjuh/FireAndRescueAcademyDiscordBot
// @version      1.0.0
// @description  Private admin tool: one dispatch center in every US state ("Z. USA - <STATE> (<XX>)") and in every country of the world ("Z. <COUNTRY>"), skipping every place you already cover. New centers are disabled and get their own coverage area.
// @match        https://www.missionchief.com/*
// @match        https://missionchief.com/*
// @grant        none
// @run-at       document-idle
// @updateURL    https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/tools/fra-dispatch-builder.user.js
// @downloadURL  https://raw.githubusercontent.com/Brandjuh/FireAndRescueAcademyDiscordBot/main/tools/fra-dispatch-builder.user.js
// ==/UserScript==

/*
 * WHAT THIS IS
 * ------------
 * The sibling of FRA Auto-Build, for dispatch centers only. It sends
 * nothing anywhere but missionchief.com (and OpenStreetMap's Nominatim, to
 * find out where your existing centers stand).
 *
 * THE GOAL
 * --------
 * - every US state has one dispatch center, named "Z. USA - TEXAS (TX)"
 *   (the format your existing state centers already use; the template is
 *   a setting);
 * - every other country of the world has one, named "Z. GERMANY";
 * - every NEW center gets, on its edit page, both
 *     [x] Disable this dispatch center
 *     [x] Create your own coverage area
 *   ticked and saved — and the save is checked by reading the page back.
 *
 * WHAT COUNTS AS "ALREADY HAS ONE"
 * --------------------------------
 * Any dispatch center of yours, whatever it is called, EXCEPT the mutual
 * aid ones ("[MUTAL AID] …", "MUTAL AID …", "Z. [MUTAL AID] …" — ignored for
 * now). A center covers:
 *   - the country / US state it STANDS in (asked once from OpenStreetMap,
 *     then cached), and
 *   - every US state its NAME claims: "KANSAS (KS) / MISSOURI (MO)" covers
 *     both, "Z. USA - OREGON (OR)" covers Oregon even though the pin is
 *     just across the border in Washington.
 * Press "Scan" to see the result before anything is built.
 *
 * HOW A CENTER IS BUILT
 * ---------------------
 * At the capital (state capital for US states), on the first spot near it
 * where the game's own address lookup returns an address. The game's build
 * form is posted the way the game posts it (credits; build_with_coins left
 * empty — a dispatch center costs 0 credits; the script refuses one that
 * costs more than "Max price"). Then the edit page: the two boxes above.
 *
 * SAFETY RAILS
 * ------------
 * - DRY RUN is on by default: it scans and reports exactly what it would
 *   build, where and under which name, and builds nothing.
 * - A per-run cap and an interval between builds; one tab drives.
 * - A target is never built twice: the exact name is checked against your
 *   buildings right before every build.
 * - Never coins, never a building complex, never another building type.
 */

(function () {
  "use strict";

  if (window.top !== window.self) return;   // never inside the game's frames

  const VERSION = "1.0.0";
  const BASE = /missionchief\.com$/i.test(window.location.hostname)
    ? window.location.origin
    : "https://www.missionchief.com";
  const SETTINGS_KEY = "fra_dispatch_settings";
  const WHERE_KEY = "fra_dispatch_where";       // center id -> {lat, lng, cc, state}
  const BUILT_KEY = "fra_dispatch_built";       // target key -> {id, name, at, optionsOk}
  const SKIP_KEY = "fra_dispatch_skipped";      // target key -> reason
  const OWNER_KEY = "fra_dispatch_owner";
  const PANEL_OPEN_KEY = "fra_dispatch_panel_open";
  const TAB_ID = Math.random().toString(36).slice(2);
  const OWNER_STALE_MS = 20000;
  const DISPATCH_TYPE = "1";                    // the game's type id
  const NOMINATIM = "https://nominatim.openstreetmap.org";
  const MUTUAL_AID_RE = /mutu?al\s*aid/i;
  const COMPLEX_RE = /complex/i;

  const DEFAULTS = {
    enabled: false,
    dryRun: true,
    doStates: true,
    doCountries: true,
    includeTerritories: false,       // Bermuda, Greenland, Puerto Rico, …
    stateTemplate: "Z. USA - {STATE} ({CODE})",
    countryTemplate: "Z. {COUNTRY}",
    setOptions: true,                // disable + own coverage area
    intervalSeconds: 20,
    maxPerRun: 25,
    maxPrice: 0,                     // credits; a dispatch center is free
  };

  // US states: [code, name, capital lat, capital lng] — Nominatim, Nov 2026.
  // Countries: [ISO code, English name, capital lat, capital lng, sovereign]
  // — the mledoze/countries list, capitals geocoded with Nominatim.
  const STATES = [
    ["AL", "Alabama", 32.3777, -86.3091],
    ["AK", "Alaska", 58.302, -134.4197],
    ["AZ", "Arizona", 33.4484, -112.0741],
    ["AR", "Arkansas", 34.7465, -92.2896],
    ["CA", "California", 38.5811, -121.4939],
    ["CO", "Colorado", 39.7392, -104.9849],
    ["CT", "Connecticut", 41.7662, -72.6733],
    ["DE", "Delaware", 39.1582, -75.5244],
    ["FL", "Florida", 30.4381, -84.2809],
    ["GA", "Georgia", 33.7545, -84.3898],
    ["HI", "Hawaii", 21.3045, -157.8557],
    ["ID", "Idaho", 43.6166, -116.2009],
    ["IL", "Illinois", 39.799, -89.644],
    ["IN", "Indiana", 39.7683, -86.1584],
    ["IA", "Iowa", 41.5869, -93.6249],
    ["KS", "Kansas", 39.049, -95.6776],
    ["KY", "Kentucky", 38.2009, -84.8733],
    ["LA", "Louisiana", 30.4494, -91.187],
    ["ME", "Maine", 44.3148, -69.7744],
    ["MD", "Maryland", 38.9786, -76.4928],
    ["MA", "Massachusetts", 42.3588, -71.0578],
    ["MI", "Michigan", 42.7338, -84.5546],
    ["MN", "Minnesota", 44.9497, -93.0931],
    ["MS", "Mississippi", 32.2999, -90.183],
    ["MO", "Missouri", 38.5774, -92.1724],
    ["MT", "Montana", 46.5927, -112.0363],
    ["NE", "Nebraska", 40.8089, -96.7078],
    ["NV", "Nevada", 39.1663, -119.767],
    ["NH", "New Hampshire", 43.2072, -71.5375],
    ["NJ", "New Jersey", 40.2203, -74.7659],
    ["NM", "New Mexico", 35.6876, -105.9385],
    ["NY", "New York", 42.6512, -73.755],
    ["NC", "North Carolina", 35.7804, -78.6391],
    ["ND", "North Dakota", 46.8083, -100.7837],
    ["OH", "Ohio", 39.9623, -83.0007],
    ["OK", "Oklahoma", 35.473, -97.5171],
    ["OR", "Oregon", 44.9392, -123.0331],
    ["PA", "Pennsylvania", 40.2663, -76.8861],
    ["RI", "Rhode Island", 41.824, -71.4128],
    ["SC", "South Carolina", 34.0008, -81.0352],
    ["SD", "South Dakota", 44.3684, -100.3511],
    ["TN", "Tennessee", 36.1623, -86.7743],
    ["TX", "Texas", 30.2649, -97.7474],
    ["UT", "Utah", 40.7596, -111.8868],
    ["VT", "Vermont", 44.2602, -72.575],
    ["VA", "Virginia", 37.5385, -77.4343],
    ["WA", "Washington", 47.0451, -122.895],
    ["WV", "West Virginia", 38.3506, -81.6333],
    ["WI", "Wisconsin", 43.0747, -89.3842],
    ["WY", "Wyoming", 41.14, -104.8202],
  ];
  const COUNTRIES = [
    ["AF", "Afghanistan", 34.527, 69.1851, true],
    ["AL", "Albania", 41.3284, 19.818, true],
    ["DZ", "Algeria", 36.7729, 3.0588, true],
    ["AS", "American Samoa", -14.2755, -170.7048, false],
    ["AD", "Andorra", 42.4979, 1.5032, true],
    ["AO", "Angola", -8.8273, 13.244, true],
    ["AI", "Anguilla", 18.2146, -63.0518, false],
    ["AG", "Antigua and Barbuda", 17.1185, -61.8449, true],
    ["AR", "Argentina", -34.6096, -58.3888, true],
    ["AM", "Armenia", 40.1777, 44.5126, true],
    ["AW", "Aruba", 12.5201, -70.0371, false],
    ["AU", "Australia", -35.2976, 149.1013, true],
    ["AT", "Austria", 48.2084, 16.3725, true],
    ["AZ", "Azerbaijan", 40.3756, 49.8328, true],
    ["BS", "Bahamas", 25.0782, -77.3383, true],
    ["BH", "Bahrain", 26.2235, 50.5822, true],
    ["BD", "Bangladesh", 23.7644, 90.389, true],
    ["BB", "Barbados", 13.0978, -59.6184, true],
    ["BY", "Belarus", 53.9025, 27.5618, true],
    ["BE", "Belgium", 50.8467, 4.3525, true],
    ["BZ", "Belize", 17.2502, -88.77, true],
    ["BJ", "Benin", 6.4991, 2.6253, true],
    ["BM", "Bermuda", 32.2943, -64.7853, false],
    ["BT", "Bhutan", 27.4714, 89.6337, true],
    ["BO", "Bolivia", -19.0477, -65.2594, true],
    ["BA", "Bosnia and Herzegovina", 43.8571, 18.4126, true],
    ["BW", "Botswana", -24.6581, 25.9088, true],
    ["BR", "Brazil", -15.794, -47.8828, true],
    ["IO", "British Indian Ocean Territory", -7.3384, 72.4718, false],
    ["VG", "British Virgin Islands", 18.4258, -64.6231, false],
    ["BN", "Brunei", 4.8895, 114.9418, true],
    ["BG", "Bulgaria", 42.6977, 23.3217, true],
    ["BF", "Burkina Faso", 12.3682, -1.5271, true],
    ["BI", "Burundi", -3.4285, 29.925, true],
    ["KH", "Cambodia", 11.573, 104.8578, true],
    ["CM", "Cameroon", 3.869, 11.5213, true],
    ["CA", "Canada", 45.4209, -75.6901, true],
    ["CV", "Cape Verde", 14.9163, -23.5095, true],
    ["KY", "Cayman Islands", 19.2954, -81.3808, false],
    ["CF", "Central African Republic", 4.3635, 18.5836, true],
    ["TD", "Chad", 12.1192, 15.0503, true],
    ["CL", "Chile", -33.4377, -70.6511, true],
    ["CN", "China", 39.9057, 116.3913, true],
    ["CX", "Christmas Island", -10.4913, 105.6171, false],
    ["CC", "Cocos (Keeling) Islands", -12.1459, 96.8423, false],
    ["CO", "Colombia", 4.6534, -74.0836, true],
    ["KM", "Comoros", -11.704, 43.2519, true],
    ["CG", "Congo", -4.2694, 15.2712, true],
    ["CK", "Cook Islands", -21.2075, -159.7708, false],
    ["CR", "Costa Rica", 9.9327, -84.0797, true],
    ["HR", "Croatia", 45.8131, 15.9773, true],
    ["CU", "Cuba", 23.1353, -82.359, true],
    ["CW", "Curaçao", 12.1067, -68.9351, false],
    ["CY", "Cyprus", 35.1747, 33.3639, true],
    ["CZ", "Czechia", 50.0875, 14.4213, true],
    ["CD", "DR Congo", -4.3197, 15.3424, true],
    ["DK", "Denmark", 55.6867, 12.5701, true],
    ["DJ", "Djibouti", 11.8146, 42.8453, true],
    ["DM", "Dominica", 15.2992, -61.3873, true],
    ["DO", "Dominican Republic", 18.4714, -69.8918, true],
    ["EC", "Ecuador", -0.2202, -78.5123, true],
    ["EG", "Egypt", 30.0444, 31.2357, true],
    ["SV", "El Salvador", 13.6976, -89.1912, true],
    ["GQ", "Equatorial Guinea", 1.5995, 10.8268, true],
    ["ER", "Eritrea", 15.339, 38.9327, true],
    ["EE", "Estonia", 59.4372, 24.7573, true],
    ["SZ", "Eswatini", -26.4463, 31.2084, true],
    ["ET", "Ethiopia", 9.0, 38.75, true],
    ["FK", "Falkland Islands", -51.6931, -57.8565, false],
    ["FO", "Faroe Islands", 62.0101, -6.7716, false],
    ["FJ", "Fiji", -18.1416, 178.4422, true],
    ["FI", "Finland", 60.1666, 24.9435, true],
    ["FR", "France", 48.8535, 2.3484, true],
    ["GF", "French Guiana", 4.9372, -52.3259, false],
    ["PF", "French Polynesia", -17.5374, -149.566, false],
    ["TF", "French Southern and Antarctic Lands", -49.3498, 70.22, false],
    ["GA", "Gabon", 0.4087, 9.4419, true],
    ["GM", "Gambia", 13.4553, -16.5756, true],
    ["GE", "Georgia", 41.6935, 44.8014, true],
    ["DE", "Germany", 52.5174, 13.3951, true],
    ["GH", "Ghana", 5.5571, -0.2012, true],
    ["GI", "Gibraltar", 36.1286, -5.3475, false],
    ["GR", "Greece", 37.9756, 23.7348, true],
    ["GL", "Greenland", 64.1767, -51.7361, false],
    ["GD", "Grenada", 12.0535, -61.7518, true],
    ["GP", "Guadeloupe", 16.0001, -61.7333, false],
    ["GU", "Guam", 13.4748, 144.7516, false],
    ["GT", "Guatemala", 14.6416, -90.5133, true],
    ["GG", "Guernsey", 49.4568, -2.539, false],
    ["GN", "Guinea", 9.5171, -13.6998, true],
    ["GW", "Guinea-Bissau", 11.8613, -15.5831, true],
    ["GY", "Guyana", 6.8033, -58.1455, true],
    ["HT", "Haiti", 18.5473, -72.3396, true],
    ["HN", "Honduras", 14.1058, -87.2047, true],
    ["HK", "Hong Kong", 22.2766, 114.1693, false],
    ["HU", "Hungary", 47.4818, 19.1449, true],
    ["IS", "Iceland", 64.146, -21.9422, true],
    ["IN", "India", 28.6139, 77.209, true],
    ["ID", "Indonesia", -6.1754, 106.8272, true],
    ["IR", "Iran", 35.6893, 51.3896, true],
    ["IQ", "Iraq", 33.3062, 44.3872, true],
    ["IE", "Ireland", 53.3494, -6.2606, true],
    ["IM", "Isle of Man", 54.1498, -4.4779, false],
    ["IL", "Israel", 31.7788, 35.2258, true],
    ["IT", "Italy", 41.8933, 12.4829, true],
    ["CI", "Ivory Coast", 6.82, -5.2776, true],
    ["JM", "Jamaica", 17.9712, -76.7928, true],
    ["JP", "Japan", 35.6769, 139.7639, true],
    ["JE", "Jersey", 49.1857, -2.1102, false],
    ["JO", "Jordan", 31.9516, 35.924, true],
    ["KZ", "Kazakhstan", 51.1283, 71.4305, true],
    ["KE", "Kenya", -1.289, 36.8173, true],
    ["KI", "Kiribati", 1.3491, 173.0387, true],
    ["XK", "Kosovo", 42.6639, 21.1641, false],
    ["KW", "Kuwait", 29.3797, 47.9734, true],
    ["KG", "Kyrgyzstan", 42.8761, 74.6037, true],
    ["LA", "Laos", 17.9641, 102.6134, true],
    ["LV", "Latvia", 56.9494, 24.1052, true],
    ["LB", "Lebanon", 33.8892, 35.5026, true],
    ["LS", "Lesotho", -29.3101, 27.4782, true],
    ["LR", "Liberia", 6.3204, -10.806, true],
    ["LY", "Libya", 32.8967, 13.1778, true],
    ["LI", "Liechtenstein", 47.1393, 9.5228, true],
    ["LT", "Lithuania", 54.687, 25.2829, true],
    ["LU", "Luxembourg", 49.8159, 6.1297, true],
    ["MG", "Madagascar", -18.91, 47.5256, true],
    ["MW", "Malawi", -13.9875, 33.7681, true],
    ["MY", "Malaysia", 3.1517, 101.6942, true],
    ["MV", "Maldives", 4.178, 73.5107, true],
    ["ML", "Mali", 12.6133, -7.985, true],
    ["MT", "Malta", 35.899, 14.5137, true],
    ["MH", "Marshall Islands", 7.091, 171.3816, true],
    ["MQ", "Martinique", 14.6028, -61.0677, false],
    ["MR", "Mauritania", 18.0792, -15.978, true],
    ["MU", "Mauritius", -20.1625, 57.5028, true],
    ["YT", "Mayotte", -12.7804, 45.228, false],
    ["MX", "Mexico", 19.3208, -99.1515, true],
    ["FM", "Micronesia", 6.9207, 158.1627, true],
    ["MD", "Moldova", 47.0245, 28.8323, true],
    ["MC", "Monaco", 43.7323, 7.4277, true],
    ["MN", "Mongolia", 47.9185, 106.9177, true],
    ["ME", "Montenegro", 42.4415, 19.2621, true],
    ["MS", "Montserrat", 16.7052, -62.2123, false],
    ["MA", "Morocco", 34.0218, -6.8409, true],
    ["MZ", "Mozambique", -25.9662, 32.5675, true],
    ["MM", "Myanmar", 19.7753, 96.1033, true],
    ["NA", "Namibia", -22.5776, 17.0773, true],
    ["NR", "Nauru", -0.5471, 166.9163, true],
    ["NP", "Nepal", 27.7083, 85.3206, true],
    ["NL", "Netherlands", 52.3731, 4.8925, true],
    ["NC", "New Caledonia", -22.2745, 166.4424, false],
    ["NZ", "New Zealand", -41.2888, 174.7772, true],
    ["NI", "Nicaragua", 12.11, -86.25, true],
    ["NE", "Niger", 13.5248, 2.1098, true],
    ["NG", "Nigeria", 9.0643, 7.4893, true],
    ["NU", "Niue", -19.0534, -169.9192, false],
    ["NF", "Norfolk Island", -29.0564, 167.9596, false],
    ["KP", "North Korea", 39.0197, 125.7526, true],
    ["MK", "North Macedonia", 41.9961, 21.4317, true],
    ["MP", "Northern Mariana Islands", 15.191, 145.7467, false],
    ["NO", "Norway", 59.9133, 10.739, true],
    ["OM", "Oman", 23.6124, 58.5938, true],
    ["PK", "Pakistan", 33.6938, 73.0652, true],
    ["PW", "Palau", 7.5006, 134.6243, true],
    ["PS", "Palestine", 31.8978, 35.1924, false],
    ["PA", "Panama", 8.9714, -79.5342, true],
    ["PG", "Papua New Guinea", -9.4743, 147.16, true],
    ["PY", "Paraguay", -25.28, -57.6344, true],
    ["PE", "Peru", -12.046, -77.0306, true],
    ["PH", "Philippines", 14.5904, 120.9804, true],
    ["PN", "Pitcairn Islands", -25.0667, -130.1002, false],
    ["PL", "Poland", 52.2334, 21.0711, true],
    ["PT", "Portugal", 38.7078, -9.1366, true],
    ["PR", "Puerto Rico", 18.3842, -66.0534, false],
    ["QA", "Qatar", 25.3109, 51.5082, true],
    ["RO", "Romania", 44.4361, 26.1027, true],
    ["RU", "Russia", 55.6256, 37.6064, true],
    ["RW", "Rwanda", -1.9534, 30.114, true],
    ["RE", "Réunion", -20.88, 55.4481, false],
    ["BL", "Saint Barthélemy", 17.8957, -62.8508, false],
    ["SH", "Saint Helena, Ascension and Tristan da Cunha", -15.9277, -5.7161, false],
    ["KN", "Saint Kitts and Nevis", 17.2961, -62.7223, true],
    ["LC", "Saint Lucia", 14.0096, -60.9902, true],
    ["MF", "Saint Martin", 18.0669, -63.0849, false],
    ["PM", "Saint Pierre and Miquelon", 46.7775, -56.1769, false],
    ["VC", "Saint Vincent and the Grenadines", 13.1562, -61.228, true],
    ["WS", "Samoa", -13.8345, -171.7631, true],
    ["SM", "San Marino", 43.9364, 12.4467, true],
    ["SA", "Saudi Arabia", 24.6389, 46.716, true],
    ["SN", "Senegal", 14.6934, -17.4479, true],
    ["RS", "Serbia", 44.8153, 20.4457, true],
    ["SC", "Seychelles", -4.6232, 55.4524, true],
    ["SL", "Sierra Leone", 8.479, -13.268, true],
    ["SG", "Singapore", 1.3571, 103.8195, true],
    ["SX", "Sint Maarten", 18.0251, -63.0483, false],
    ["SK", "Slovakia", 48.1435, 17.1083, true],
    ["SI", "Slovenia", 46.05, 14.5069, true],
    ["SB", "Solomon Islands", -9.4311, 159.9553, true],
    ["SO", "Somalia", 2.0349, 45.3419, true],
    ["ZA", "South Africa", -25.7459, 28.1879, true],
    ["GS", "South Georgia", -54.2835, -36.4946, false],
    ["KR", "South Korea", 37.5667, 126.9783, true],
    ["SS", "South Sudan", 4.8459, 31.5959, true],
    ["ES", "Spain", 40.4168, -3.7035, true],
    ["LK", "Sri Lanka", 6.9389, 79.8542, true],
    ["SD", "Sudan", 15.5636, 32.5349, true],
    ["SR", "Suriname", 5.8242, -55.1663, true],
    ["SJ", "Svalbard and Jan Mayen", 78.2232, 15.6464, false],
    ["SE", "Sweden", 59.3251, 18.0711, true],
    ["CH", "Switzerland", 46.9485, 7.4522, true],
    ["SY", "Syria", 33.5131, 36.3096, true],
    ["ST", "São Tomé and Príncipe", 0.3389, 6.7313, true],
    ["TW", "Taiwan", 25.0375, 121.5637, false],
    ["TJ", "Tajikistan", 38.5767, 68.7854, true],
    ["TZ", "Tanzania", -6.1791, 35.7468, true],
    ["TH", "Thailand", 13.7525, 100.4935, true],
    ["TL", "Timor-Leste", -8.5537, 125.5784, true],
    ["TG", "Togo", 6.1304, 1.2158, true],
    ["TK", "Tokelau", -9.3736, -171.2415, false],
    ["TO", "Tonga", -21.1343, -175.2018, true],
    ["TT", "Trinidad and Tobago", 10.6573, -61.518, true],
    ["TN", "Tunisia", 36.8002, 10.1858, true],
    ["TM", "Turkmenistan", 37.9405, 58.3823, true],
    ["TC", "Turks and Caicos Islands", 21.4608, -71.14, false],
    ["TV", "Tuvalu", -8.52, 179.1983, true],
    ["TR", "Türkiye", 39.9208, 32.854, true],
    ["UG", "Uganda", 0.3177, 32.5814, true],
    ["UA", "Ukraine", 50.45, 30.5241, true],
    ["AE", "United Arab Emirates", 24.4538, 54.3774, true],
    ["GB", "United Kingdom", 51.5074, -0.1278, true],
    ["VI", "United States Virgin Islands", 18.3369, -64.9622, false],
    ["UY", "Uruguay", -34.9059, -56.1913, true],
    ["UZ", "Uzbekistan", 41.3123, 69.2787, true],
    ["VU", "Vanuatu", -17.7415, 168.315, true],
    ["VA", "Vatican City", 41.9034, 12.4529, true],
    ["VE", "Venezuela", 10.5061, -66.9146, true],
    ["VN", "Vietnam", 21.0283, 105.854, true],
    ["WF", "Wallis and Futuna", -13.282, -176.174, false],
    ["EH", "Western Sahara", 26.7301, -12.9767, false],
    ["YE", "Yemen", 15.3539, 44.2059, true],
    ["ZM", "Zambia", -15.4163, 28.2818, true],
    ["ZW", "Zimbabwe", -17.8567, 31.0602, true],
    ["AX", "Åland Islands", 60.1024, 19.9413, false],
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
      /* private mode: nothing persists */
    }
  }

  let settings = Object.assign({}, DEFAULTS, readJson(SETTINGS_KEY, {}));
  let where = readJson(WHERE_KEY, {});
  let built = readJson(BUILT_KEY, {});
  let skipped = readJson(SKIP_KEY, {});
  const saveSettings = () => writeJson(SETTINGS_KEY, settings);
  const saveWhere = () => writeJson(WHERE_KEY, where);
  const saveBuilt = () => writeJson(BUILT_KEY, built);
  const saveSkipped = () => writeJson(SKIP_KEY, skipped);

  const state = {
    running: false,
    busy: false,
    nextAt: 0,
    runCount: 0,
    scan: null,            // last scan result
    logLines: [],
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function log(message, kind) {
    state.logLines.push({
      at: new Date().toLocaleTimeString(), text: String(message), kind: kind || "info",
    });
    if (state.logLines.length > 400) state.logLines.shift();
    (kind === "error" ? console.warn : console.info)("[FRA Dispatch]", message);
    renderLog();
  }

  function normalize(text) {
    return String(text || "")
      .normalize("NFD").replace(/[̀-ͯ]/g, "")
      .toUpperCase().replace(/\s+/g, " ").trim();
  }

  // ------------------------------------------------------------ the game

  function isLoginPage(doc) {
    return !!doc.querySelector('input[type="password"]');
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
      throw new Error("STOP — you are logged out of MissionChief");
    }
    if (!page.response.ok) throw new Error(`${path} -> HTTP ${page.response.status}`);
    return page.doc;
  }

  async function fetchJson(path) {
    const response = await fetch(BASE + path, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
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
    // A Rails form post, sent the way the game's own jQuery sends it.
    const body = params instanceof URLSearchParams ? params : new URLSearchParams(params || {});
    if (token && !body.has("authenticity_token")) body.set("authenticity_token", token);
    const headers = {
      "X-Requested-With": "XMLHttpRequest",
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    };
    if (token) headers["X-CSRF-Token"] = token;
    return fetchPage(path, { method: "POST", headers, body: body.toString() });
  }

  function formParams(form) {
    // What jQuery's $(form).serialize() sends: named, enabled controls,
    // no buttons/files, unticked boxes left out, in page order. Order
    // matters for Rails' hidden "0" + checkbox "1" pairs: the last wins.
    const params = new URLSearchParams();
    for (const field of form.elements) {
      if (!field.name || field.disabled) continue;
      const type = String(field.type || "").toLowerCase();
      if (["submit", "button", "image", "reset", "file"].includes(type)) continue;
      if ((type === "checkbox" || type === "radio") && !field.checked) continue;
      params.append(field.name, field.value === undefined ? "" : String(field.value));
    }
    return params;
  }

  function formErrors(doc) {
    const el = [...doc.querySelectorAll("#error_explanation, .has-error .help-block")]
      .find((candidate) => !candidate.closest("#building_complex, .hidden"));
    return el ? (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 300) : "";
  }

  async function readCredits() {
    try {
      const data = await fetchJson("/api/credits");
      const value = parseInt(data && data.credits_user_current, 10);
      return isFinite(value) ? value : null;
    } catch (error) {
      return null;
    }
  }

  async function dispatchCenters() {
    const rows = await fetchJson("/api/buildings");
    return (Array.isArray(rows) ? rows : [])
      .filter((row) => String(row.building_type) === DISPATCH_TYPE)
      .map((row) => ({
        id: String(row.id),
        name: String(row.caption || row.name || "").trim(),
        lat: parseFloat(row.latitude),
        lng: parseFloat(row.longitude),
      }));
  }

  async function gameAddress(lat, lng) {
    try {
      const response = await fetch(
        `${BASE}/reverse_address?latitude=${lat.toFixed(6)}&longitude=${lng.toFixed(6)}`,
        { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
      if (!response.ok) return "";
      const text = (await response.text()).replace(/\s+/g, " ").trim();
      if (!text || /<[a-z!]/i.test(text)) return "";
      return text.slice(0, 200);
    } catch (error) {
      return "";
    }
  }

  // ------------------------------------------------------- where is what

  let lastNominatimAt = 0;

  async function nominatimReverse(lat, lng) {
    // Nominatim allows one request per second; this tool asks rarely.
    const wait = lastNominatimAt + 1100 - Date.now();
    if (wait > 0) await sleep(wait);
    lastNominatimAt = Date.now();
    const response = await fetch(
      `${NOMINATIM}/reverse?format=jsonv2&zoom=8&accept-language=en` +
      `&lat=${lat.toFixed(5)}&lon=${lng.toFixed(5)}`,
      { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`OpenStreetMap -> HTTP ${response.status}`);
    const data = await response.json();
    const address = (data && data.address) || {};
    const iso = String(address["ISO3166-2-lvl4"] || "");
    return {
      cc: String(address.country_code || "").toUpperCase(),
      state: /^US-[A-Z]{2}$/.test(iso) ? iso.slice(3) : "",
      text: [address.state, address.country].filter(Boolean).join(", "),
    };
  }

  async function locate(center) {
    // Cached per center; re-asked only when the center moved.
    const cached = where[center.id];
    if (cached && Math.abs(cached.lat - center.lat) < 1e-4
        && Math.abs(cached.lng - center.lng) < 1e-4 && cached.cc) {
      return cached;
    }
    const found = await nominatimReverse(center.lat, center.lng);
    where[center.id] = Object.assign({ lat: center.lat, lng: center.lng }, found);
    saveWhere();
    return where[center.id];
  }

  function statesInName(name) {
    // "Z. USA - KANSAS (KS) / MISSOURI (MO)" -> KS, MO;
    // "Z. USA - NEW YORK / CONNECTICUT(NY / CT)" -> NY, CT.
    // Only for names that say USA, and whole state names only — longest
    // first, so "WEST VIRGINIA" never also counts as "VIRGINIA".
    const upper = normalize(name);
    if (!/\bUSA\b|\bUS\b|UNITED STATES/.test(upper)) return [];
    const codes = new Set();
    const known = new Set(STATES.map((entry) => entry[0]));
    for (const group of upper.match(/\(([^)]*)\)/g) || []) {
      for (const token of group.split(/[^A-Z]+/)) {
        if (known.has(token)) codes.add(token);
      }
    }
    let rest = upper.replace(/\([^)]*\)/g, " ");
    const byLength = STATES.slice().sort((a, b) => b[1].length - a[1].length);
    for (const [code, stateName] of byLength) {
      const pattern = new RegExp(`\\b${normalize(stateName)}\\b`);
      if (pattern.test(rest)) {
        codes.add(code);
        rest = rest.replace(pattern, " ");
      }
    }
    return [...codes];
  }

  function targetName(target) {
    if (target.kind === "state") {
      return settings.stateTemplate
        .replace("{STATE}", target.name.toUpperCase())
        .replace("{CODE}", target.code);
    }
    return settings.countryTemplate.replace("{COUNTRY}", target.name.toUpperCase());
  }

  function allTargets() {
    const list = [];
    if (settings.doStates) {
      for (const [code, name, lat, lng] of STATES) {
        list.push({ key: `US-${code}`, kind: "state", code, name, lat, lng });
      }
    }
    if (settings.doCountries) {
      for (const [code, name, lat, lng, sovereign] of COUNTRIES) {
        if (!sovereign && !settings.includeTerritories) continue;
        list.push({ key: code, kind: "country", code, name, lat, lng, sovereign });
      }
    }
    for (const target of list) target.buildName = targetName(target);
    return list;
  }

  async function scan(quiet) {
    // Which states/countries are covered, by which center, and what is left.
    const centers = await dispatchCenters();
    const mutual = centers.filter((center) => MUTUAL_AID_RE.test(center.name));
    const own = centers.filter((center) => !MUTUAL_AID_RE.test(center.name));
    const coveredBy = {};                  // target key -> [center names]
    const cover = (key, center, how) => {
      (coveredBy[key] = coveredBy[key] || []).push(`${center.name} (${how})`);
    };
    let unknown = 0;
    let asked = 0;
    for (const center of own) {
      for (const code of statesInName(center.name)) cover(`US-${code}`, center, "name");
      if (!isFinite(center.lat) || !isFinite(center.lng)) continue;
      try {
        if (!where[center.id] || where[center.id].lat !== center.lat) asked += 1;
        const spot = await locate(center);
        if (spot.cc === "US" && spot.state) cover(`US-${spot.state}`, center, "stands there");
        else if (spot.cc) cover(spot.cc, center, "stands there");
        else unknown += 1;
      } catch (error) {
        unknown += 1;
        if (!quiet) log(`⚠️ could not place "${center.name}": ${error.message}`, "error");
      }
      renderScanProgress(own.indexOf(center) + 1, own.length);
    }
    // An exact name match counts too (a center may stand just across a border).
    const byName = new Map(own.map((center) => [normalize(center.name), center]));
    const targets = allTargets();
    for (const target of targets) {
      const twin = byName.get(normalize(target.buildName));
      if (twin) cover(target.key, twin, "same name");
    }
    const missing = targets.filter((target) => !coveredBy[target.key]);
    state.scan = { at: Date.now(), centers, mutual, own, coveredBy, targets, missing, unknown };
    if (!quiet) {
      const states = targets.filter((t) => t.kind === "state");
      const countries = targets.filter((t) => t.kind === "country");
      log(`🔎 ${own.length} dispatch centers of yours (+${mutual.length} mutual aid, ignored)` +
          (asked ? `, ${asked} newly placed via OpenStreetMap` : "") +
          (unknown ? `, ${unknown} could not be placed` : ""));
      log(`   US states: ${states.length - missing.filter((t) => t.kind === "state").length}` +
          `/${states.length} covered · countries: ` +
          `${countries.length - missing.filter((t) => t.kind === "country").length}` +
          `/${countries.length} covered · ${missing.length} to build`, "ok");
    }
    renderScan();
    return state.scan;
  }

  // ------------------------------------------------------------ building

  async function findSpot(target) {
    // The capital itself first, then rings around it, until the game's own
    // address lookup knows the spot (empty = water or nowhere).
    const rings = [[0, 0]];
    for (const km of [0.5, 1.5, 3, 6]) {
      for (let i = 0; i < 4; i++) rings.push([km, Math.random() * 2 * Math.PI]);
    }
    for (const [km, bearing] of rings) {
      const lat = target.lat + (km * Math.cos(bearing)) / 111;
      const lng = target.lng + (km * Math.sin(bearing))
        / (111 * Math.max(0.2, Math.cos((target.lat * Math.PI) / 180)));
      const address = await gameAddress(lat, lng);
      if (address) return { lat, lng, address };
    }
    return null;
  }

  async function buildTarget(target) {
    const name = targetName(target);
    // Right before building: is it there already (built by hand, another tab)?
    const centers = await dispatchCenters();
    const twin = centers.find((center) => normalize(center.name) === normalize(name));
    if (twin) {
      built[target.key] = built[target.key] || { id: twin.id, name, at: Date.now(), existed: true };
      saveBuilt();
      log(`⏭️ ${name} already exists (#${twin.id})`);
      return { skipped: true };
    }

    const doc = await fetchDoc("/buildings/new");
    const form = doc.querySelector("#new_building");
    if (!form) throw new Error("STOP — the build form has no #new_building any more");
    const typeSelect = form.querySelector('[name="building[building_type]"]');
    const typeOption = typeSelect && [...typeSelect.options]
      .find((option) => option.value === DISPATCH_TYPE);
    if (!typeOption || !/dispatch/i.test(typeOption.textContent || "")) {
      throw new Error(`STOP — type ${DISPATCH_TYPE} on the build form is not a dispatch center`);
    }
    const button = doc.getElementById(`build_credits_${DISPATCH_TYPE}`);
    const label = button ? String(button.value || button.textContent || "").trim() : "";
    if (!button || !/credits/i.test(label) || /coin/i.test(label)
        || button.classList.contains("coins_activate")
        || button.classList.contains("alliance_activate")
        || button.disabled || button.classList.contains("btn-danger")) {
      throw new Error(`STOP — no usable personal credits button for a dispatch center ("${label}")`);
    }
    const price = parseInt(label.replace(/[^\d]/g, ""), 10) || 0;
    if (price > (Number(settings.maxPrice) || 0)) {
      throw new Error(`STOP — a dispatch center costs ${price.toLocaleString()} credits now, ` +
        `above your max price ${Number(settings.maxPrice).toLocaleString()}`);
    }

    const spot = await findSpot(target);
    if (!spot) {
      skipped[target.key] = "no address near the capital";
      saveSkipped();
      log(`⚠️ ${name}: the game knows no address near ${target.lat}, ${target.lng} — skipped`,
          "error");
      return { skipped: true };
    }

    const params = formParams(form);
    params.set("building[building_type]", DISPATCH_TYPE);
    params.set("building[name]", name);
    params.set("building[latitude]", spot.lat.toFixed(6));
    params.set("building[longitude]", spot.lng.toFixed(6));
    params.set("building[address]", spot.address);
    params.set("build_with_coins", "");          // credits — as the game sends it
    params.set("build_as_alliance", "");         // personal
    params.delete("build_another");
    for (const key of [...params.keys()]) {
      if (COMPLEX_RE.test(key)) params.delete(key);
    }
    if (params.get("build_with_coins") !== "" || params.get("build_as_alliance") !== "") {
      throw new Error("STOP — refusing to build: coins/alliance fields are not empty");
    }

    const summary = `"${name}" at ${spot.lat.toFixed(4)}, ${spot.lng.toFixed(4)} ` +
      `(${spot.address}; ${label})`;
    if (settings.dryRun) {
      log(`🧪 DRY RUN — would build ${summary}` +
          (settings.setOptions ? ", then tick Disable + Own coverage area" : ""), "ok");
      return { dryRun: true };
    }

    const before = new Set(centers.map((center) => center.id));
    const sent = await send(form.getAttribute("action") || "/buildings", params, csrfToken(doc));
    if (/\/users\/sign_(in|up)/.test(sent.url) || isLoginPage(sent.doc)) {
      throw new Error("STOP — you are logged out of MissionChief");
    }
    if (!sent.response.ok) throw new Error(`the game refused ${name} — HTTP ${sent.response.status}`);
    let id = null;
    for (let attempt = 0; attempt < 3 && !id; attempt++) {
      if (attempt) await sleep(2500);
      const now = await dispatchCenters();
      const fresh = now.filter((center) => !before.has(center.id)
        && Math.abs(center.lat - spot.lat) < 0.003 && Math.abs(center.lng - spot.lng) < 0.003);
      if (fresh.length) id = fresh[0].id;
    }
    if (!id) throw new Error(`${name}: the build did not confirm — no new dispatch center at the pin`);
    built[target.key] = { id, name, at: Date.now(), optionsOk: false };
    saveBuilt();
    log(`✅ built #${id} ${summary}`, "ok");
    if (settings.setOptions) await applyOptions(target.key);
    return { id };
  }

  // -------------------------------------------- disable + own coverage area

  function optionBoxes(form) {
    // Read live (Nov 2026), on a dispatch center's edit page:
    //   hidden building[active]=true + checkbox building[active]=false
    //       "Disable this dispatch center (associated stations will no
    //        longer generate missions)"
    //   hidden building[generate_own_missions]=0 + checkbox …=1
    //       "Create your own coverage area. …"
    const byLabel = (re) => [...form.querySelectorAll('input[type="checkbox"]')].find((box) => {
      const label = box.id && form.querySelector(`label[for="${box.id}"]`);
      return label && re.test(label.textContent || "");
    });
    return {
      disable: form.querySelector('input[type="checkbox"][name="building[active]"]')
        || byLabel(/disable this dispatch center/i),
      coverage: form.querySelector('input[type="checkbox"][name="building[generate_own_missions]"]')
        || byLabel(/own coverage area/i),
    };
  }

  async function readOptions(id) {
    const doc = await fetchDoc(`/buildings/${id}/edit`);
    const form = doc.querySelector(`form[action$="/buildings/${id}"]`);
    if (!form) throw new Error(`#${id}: no edit form`);
    return { doc, form, boxes: optionBoxes(form) };
  }

  async function applyOptions(key) {
    const record = built[key];
    if (!record || !record.id) return false;
    const id = record.id;
    try {
      const { doc, form, boxes } = await readOptions(id);
      if (!boxes.disable || !boxes.coverage) {
        throw new Error("the Disable / Own coverage area boxes are not on the edit page");
      }
      if (boxes.disable.checked && boxes.coverage.checked) {
        record.optionsOk = true;
        saveBuilt();
        log(`☑️ #${id} ${record.name}: Disable + Own coverage area already ticked`);
        return true;
      }
      if (settings.dryRun) {
        log(`🧪 DRY RUN — would tick Disable + Own coverage area on #${id} ${record.name}`, "ok");
        return false;
      }
      boxes.disable.checked = true;
      boxes.coverage.checked = true;
      const params = formParams(form);
      for (const name of [...params.keys()]) {
        if (COMPLEX_RE.test(name)) params.delete(name);
      }
      const sent = await send(form.getAttribute("action"), params, csrfToken(doc));
      const error = formErrors(sent.doc);
      if (!sent.response.ok || error) {
        throw new Error(`not saved (HTTP ${sent.response.status}${error ? `: ${error}` : ""})`);
      }
      // Read it back: only a page that SHOWS both ticked counts.
      const check = await readOptions(id);
      const ok = !!(check.boxes.disable && check.boxes.disable.checked
        && check.boxes.coverage && check.boxes.coverage.checked);
      record.optionsOk = ok;
      record.optionsTries = (record.optionsTries || 0) + 1;
      saveBuilt();
      log(ok ? `☑️ #${id} ${record.name}: Disable + Own coverage area saved`
             : `⚠️ #${id} ${record.name}: saved, but the page does not show both boxes ticked`,
          ok ? "ok" : "error");
      return ok;
    } catch (error) {
      record.optionsTries = (record.optionsTries || 0) + 1;
      saveBuilt();
      log(`⚠️ #${id} ${record.name}: options — ${error.message}`, "error");
      return false;
    }
  }

  async function retryOptions() {
    // New centers whose two boxes did not stick yet (at most 5 tries each).
    for (const [key, record] of Object.entries(built)) {
      if (record.existed || record.optionsOk || (record.optionsTries || 0) >= 5) continue;
      await applyOptions(key);
    }
  }

  // ------------------------------------------------------------ the loop

  function claimOwner(force) {
    const now = Date.now();
    const owner = readJson(OWNER_KEY, null);
    if (!force && owner && owner.tab !== TAB_ID && now - (owner.at || 0) < OWNER_STALE_MS) {
      return false;
    }
    writeJson(OWNER_KEY, { tab: TAB_ID, at: now });
    return true;
  }

  function releaseOwner() {
    const owner = readJson(OWNER_KEY, null);
    if (owner && owner.tab === TAB_ID) writeJson(OWNER_KEY, { tab: TAB_ID, at: 0 });
  }

  function nextTarget() {
    if (!state.scan) return null;
    return state.scan.missing.find((target) =>
      !built[target.key] && !skipped[target.key]) || null;
  }

  async function tick() {
    if (!state.running || state.busy || Date.now() < state.nextAt) return;
    if (!claimOwner()) {
      log("another tab took the dispatch builder over — stopped here", "error");
      state.running = false;
      renderStatus();
      return;
    }
    state.busy = true;
    try {
      await retryOptions();
      if (!state.scan || Date.now() - state.scan.at > 10 * 60 * 1000) await scan(true);
      if (state.runCount >= (Number(settings.maxPerRun) || 0)) {
        log(`⏹️ run cap reached (${settings.maxPerRun}) — stopped`, "ok");
        stop();
        return;
      }
      const target = nextTarget();
      if (!target) {
        log("🏁 nothing left to build — every state and country is covered", "ok");
        stop();
        return;
      }
      const result = await buildTarget(target);
      if (result.dryRun) {
        skipped[target.key] = "dry run";     // walk the whole list once, then stop
      }
      if (!result.skipped) state.runCount += 1;
    } catch (error) {
      log(`❌ ${error.message}`, "error");
      if (/^STOP —/.test(error.message)) stop();
    } finally {
      state.nextAt = Date.now() + Math.max(5, Number(settings.intervalSeconds) || 20) * 1000;
      state.busy = false;
      renderStatus();
    }
  }

  function start() {
    claimOwner(true);
    // Dry-run marks are only for walking the list once; a new run starts clean.
    for (const [key, reason] of Object.entries(skipped)) {
      if (reason === "dry run") delete skipped[key];
    }
    saveSkipped();
    state.running = true;
    state.runCount = 0;
    state.nextAt = 0;
    settings.enabled = true;
    saveSettings();
    log(`▶️ started — ${settings.dryRun ? "DRY RUN" : "LIVE"}`, "ok");
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

  // ------------------------------------------------------------------ UI

  const STYLE = `
    #fra-dc { position: fixed; right: 352px; bottom: 12px; width: 340px; z-index: 2147483600;
      font: 12px/1.45 system-ui, sans-serif; color: #eee; background: #1f2226;
      border: 1px solid #2c6fbb; border-radius: 8px; box-shadow: 0 6px 24px rgba(0,0,0,.45); }
    #fra-dc header { display: flex; align-items: center; justify-content: space-between;
      padding: 7px 10px; background: #2c6fbb; border-radius: 7px 7px 0 0; font-weight: 700; }
    #fra-dc header button { background: transparent; border: 0; color: #fff; cursor: pointer;
      font-size: 14px; padding: 0 4px; }
    #fra-dc .body { padding: 8px 10px 10px; max-height: 72vh; overflow: auto; }
    #fra-dc .row { display: flex; gap: 6px; align-items: center; margin: 4px 0; }
    #fra-dc .row > label { flex: 0 0 110px; color: #b9c0c8; }
    #fra-dc input[type=text], #fra-dc input[type=number] { flex: 1 1 auto; min-width: 0;
      background: #2b3036; color: #eee; border: 1px solid #414852; border-radius: 4px;
      padding: 3px 5px; font: inherit; }
    #fra-dc .checks { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 8px; margin: 6px 0; }
    #fra-dc .checks label { display: flex; gap: 5px; align-items: center; color: #cfd6dd; }
    #fra-dc .buttons { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 6px; }
    #fra-dc .buttons button { flex: 1 1 auto; background: #313841; color: #eee;
      border: 1px solid #4a525c; border-radius: 4px; padding: 5px 8px; cursor: pointer; font: inherit; }
    #fra-dc .buttons button.go { background: #1e7d34; border-color: #29a145; font-weight: 700; }
    #fra-dc .buttons button.stop { background: #a02c22; border-color: #c0392b; font-weight: 700; }
    #fra-dc .status { padding: 5px 6px; background: #171a1d; border-radius: 4px; color: #9fd3ae; }
    #fra-dc .scan { margin-top: 6px; max-height: 160px; overflow: auto; background: #171a1d;
      border-radius: 4px; padding: 5px 6px; font: 11px/1.4 ui-monospace, monospace; color: #cfd6dd; }
    #fra-dc .scan b { color: #fff; }
    #fra-dc .log { margin-top: 6px; height: 150px; overflow: auto; background: #15181b;
      border-radius: 4px; padding: 5px 6px; font: 11px/1.4 ui-monospace, monospace; }
    #fra-dc .log div { white-space: pre-wrap; word-break: break-word; }
    #fra-dc .log .error { color: #ff9a8f; }
    #fra-dc .log .ok { color: #8fe6a4; }
    #fra-dc .log .info { color: #c6cdd4; }
  `;

  let panel = null;
  let menuItem = null;
  let statusBox = null;
  let scanBox = null;
  let logBox = null;
  let startButton = null;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children || []) node.appendChild(child);
    return node;
  }

  function checkbox(key, text) {
    const input = el("input", { type: "checkbox" });
    input.checked = !!settings[key];
    input.addEventListener("change", () => {
      settings[key] = input.checked;
      saveSettings();
      if (["doStates", "doCountries", "includeTerritories"].includes(key)) state.scan = null;
      renderScan();
      renderStatus();
    });
    return el("label", {}, [input, el("span", { text })]);
  }

  function field(key, type) {
    const input = el("input", { type });
    input.value = String(settings[key]);
    input.addEventListener("change", () => {
      if (type === "number") {
        const value = parseFloat(input.value);
        settings[key] = isFinite(value) ? value : DEFAULTS[key];
      } else {
        settings[key] = input.value.trim() || DEFAULTS[key];
      }
      input.value = String(settings[key]);
      saveSettings();
      state.scan = null;
      renderScan();
    });
    return input;
  }

  function busyButton(text, action) {
    const button = el("button", { text });
    button.addEventListener("click", async () => {
      if (state.busy) return log("busy — one thing at a time", "error");
      state.busy = true;
      try {
        await action();
      } catch (error) {
        log(`❌ ${error.message}`, "error");
      } finally {
        state.busy = false;
        renderStatus();
      }
    });
    return button;
  }

  function renderStatus() {
    if (startButton) {
      startButton.textContent = state.running ? "Stop" : "Start";
      startButton.className = state.running ? "stop" : "go";
    }
    if (menuItem) {
      menuItem.firstChild.textContent = "🛰️ Dispatch Builder" +
        (state.running ? (settings.dryRun ? " ● dry run" : " ● LIVE") : "");
    }
    if (!statusBox) return;
    const pending = Object.values(built).filter((r) => !r.existed && !r.optionsOk).length;
    const parts = [
      state.running ? "<b>RUNNING</b>" : "idle",
      settings.dryRun ? "DRY RUN" : "<b>LIVE</b>",
      `built this run ${state.runCount}/${settings.maxPerRun}`,
    ];
    if (pending) parts.push(`${pending} still need their boxes ticked`);
    if (state.running && state.nextAt > Date.now()) {
      parts.push(`next in ${Math.ceil((state.nextAt - Date.now()) / 1000)}s`);
    }
    statusBox.innerHTML = parts.join(" · ");
  }

  function renderScanProgress(done, total) {
    if (scanBox) scanBox.textContent = `placing your dispatch centers… ${done}/${total}`;
  }

  function renderScan() {
    if (!scanBox) return;
    const scanned = state.scan;
    if (!scanned) {
      scanBox.textContent = "Press Scan to see which states and countries still need one.";
      return;
    }
    const missingStates = scanned.missing.filter((t) => t.kind === "state");
    const missingCountries = scanned.missing.filter((t) => t.kind === "country");
    const lines = [
      [`Missing US states (${missingStates.length}): `,
        missingStates.map((t) => t.code).join(", ") || "none"],
      [`Missing countries (${missingCountries.length}): `,
        missingCountries.map((t) => t.name).join(", ") || "none"],
      [`Mutual aid, ignored (${scanned.mutual.length}): `,
        scanned.mutual.map((c) => c.name).join(" · ") || "none"],
    ];
    scanBox.innerHTML = "";
    for (const [head, text] of lines) {
      scanBox.appendChild(el("div", {}, [el("b", { text: head }), document.createTextNode(text)]));
    }
  }

  function renderLog() {
    if (!logBox) return;
    const atBottom = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 20;
    logBox.innerHTML = "";
    for (const line of state.logLines.slice(-150)) {
      logBox.appendChild(el("div", { class: line.kind, text: `${line.at} ${line.text}` }));
    }
    if (atBottom) logBox.scrollTop = logBox.scrollHeight;
  }

  function coverageReport() {
    const scanned = state.scan;
    if (!scanned) return "no scan yet";
    const lines = [`FRA Dispatch Builder ${VERSION} — ${new Date().toISOString()}`];
    for (const target of scanned.targets) {
      const by = scanned.coveredBy[target.key];
      lines.push(`${by ? "OK  " : "MISS"} ${target.key.padEnd(5)} ${target.name}` +
        (by ? ` — ${by.join("; ")}` : ` — would be "${target.buildName}"`));
    }
    return lines.join("\n");
  }

  function buildPanel() {
    document.head.appendChild(el("style", { text: STYLE }));
    const body = el("div", { class: "body" });
    body.appendChild(el("div", { class: "row" }, [el("label", { text: "State name" }),
      field("stateTemplate", "text")]));
    body.appendChild(el("div", { class: "row" }, [el("label", { text: "Country name" }),
      field("countryTemplate", "text")]));
    body.appendChild(el("div", { class: "row" }, [el("label", { text: "Every (seconds)" }),
      field("intervalSeconds", "number")]));
    body.appendChild(el("div", { class: "row" }, [el("label", { text: "Max this run" }),
      field("maxPerRun", "number")]));
    body.appendChild(el("div", { class: "row" }, [el("label", { text: "Max price" }),
      field("maxPrice", "number")]));
    body.appendChild(el("div", { class: "checks" }, [
      checkbox("dryRun", "Dry run"),
      checkbox("setOptions", "Disable + own area"),
      checkbox("doStates", "US states"),
      checkbox("doCountries", "Countries"),
      checkbox("includeTerritories", "Territories too"),
    ]));

    startButton = el("button", { class: "go", text: "Start" });
    startButton.addEventListener("click", () => (state.running ? stop() : start()));
    const scanButton = busyButton("Scan", () => scan(false));
    const oneButton = busyButton("Build one", async () => {
      if (!state.scan) await scan(false);
      const target = nextTarget();
      if (!target) return log("nothing left to build", "ok");
      await buildTarget(target);
    });
    const optionsButton = busyButton("Retry boxes", retryOptions);
    const copyButton = el("button", { text: "Copy report" });
    copyButton.addEventListener("click", async () => {
      const text = coverageReport() + "\n\n" +
        state.logLines.map((line) => `${line.at} ${line.text}`).join("\n");
      try {
        await navigator.clipboard.writeText(text);
        log("📋 coverage report + log copied", "ok");
      } catch (error) {
        window.prompt("Copy this:", text.slice(0, 6000));
      }
    });
    const resetButton = el("button", { text: "Forget skips" });
    resetButton.addEventListener("click", () => {
      skipped = {};
      saveSkipped();
      log("skip list cleared", "ok");
    });
    body.appendChild(el("div", { class: "buttons" },
      [startButton, scanButton, oneButton, optionsButton, copyButton, resetButton]));
    statusBox = el("div", { class: "status" });
    body.appendChild(statusBox);
    scanBox = el("div", { class: "scan" });
    body.appendChild(scanBox);
    logBox = el("div", { class: "log" });
    body.appendChild(logBox);

    const close = el("button", { text: "×", title: "hide (reopen via the FRA menu)" });
    close.addEventListener("click", () => setPanelOpen(false));
    panel = el("div", { id: "fra-dc" }, [
      el("header", {}, [el("span", { text: `FRA Dispatch Builder ${VERSION}` }), close]),
      body,
    ]);
    document.body.appendChild(panel);
    setPanelOpen(readJson(PANEL_OPEN_KEY, false) === true);
    renderStatus();
    renderScan();
    renderLog();
  }

  function setPanelOpen(open) {
    if (panel) panel.style.display = open ? "" : "none";
    writeJson(PANEL_OPEN_KEY, open);
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

  function addMenuItem() {
    menuItem = fraMenuAdd({
      id: "fra-dispatch-menu",
      label: "🛰️ Dispatch Builder",
      order: 51,
      title: "Show/hide the FRA Dispatch Builder panel",
      onClick: () => setPanelOpen(!panel || panel.style.display === "none"),
    });
    renderStatus();
  }

  // ---------------------------------------------------------------- boot

  function boot() {
    if (document.getElementById("fra-dc")) return;
    buildPanel();
    addMenuItem();
    log(`ready — ${STATES.length} states, ${COUNTRIES.filter((c) => c[4]).length} countries ` +
        `(+${COUNTRIES.filter((c) => !c[4]).length} territories) known; ` +
        `${Object.keys(built).length} built by this tool so far`);
    if (settings.enabled) {
      if (claimOwner()) {
        state.running = true;
        state.nextAt = Date.now() + 5000;
        log("▶️ resumed after a page load");
      } else {
        log("the builder is on, but another tab is driving it", "error");
      }
    }
    setInterval(() => {
      renderStatus();
      if (state.running) {
        claimOwner();
        tick();
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
