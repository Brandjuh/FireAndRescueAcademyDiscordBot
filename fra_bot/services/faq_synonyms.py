"""MissionChief FAQ synonym dictionary (reference bot: faqmanager).

Ported VERBATIM from the reference cog — game terminology (arr, poi,
coins, …) mapped to the phrasings members actually type. Used to expand
search queries before fuzzy matching.
"""

from __future__ import annotations

DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "arr": [
        "alarm and response regulation",
        "alarm & response regulation",
        "alarm and response rules",
        "alarm response",
        "arr rules",
        "a&r",
        "arr setup",
        "alarm setup",
        "alarm rule"
    ],
    "poi": [
        "points of interest",
        "point of interest",
        "location marker",
        "mission spawn point",
        "spawn area",
        "poi marker"
    ],
    "credits": [
        "money",
        "cash",
        "income",
        "reward",
        "mission payout",
        "earnings",
        "credit",
        "payout"
    ],
    "tax": [
        "alliance tax",
        "donation",
        "contribution",
        "percentage",
        "alliance fee",
        "member tax"
    ],
    "training": [
        "education",
        "course",
        "schooling",
        "class",
        "academy",
        "classroom",
        "trainings"
    ],
    "expansion": [
        "building expansion",
        "building extension",
        "extra space",
        "station expansion",
        "expansions"
    ],
    "dispatch": [
        "control center",
        "dispatch center",
        "alarm center",
        "command center",
        "control building",
        "dispatch building"
    ],
    "vehicle": [
        "truck",
        "engine",
        "unit",
        "apparatus",
        "car",
        "van",
        "vehicle type",
        "vehicles"
    ],
    "station": [
        "building",
        "fire station",
        "ems station",
        "police station",
        "prison",
        "medical building",
        "facility"
    ],
    "missions": [
        "calls",
        "incidents",
        "jobs",
        "tasks",
        "mission list",
        "mission type"
    ],
    "shared missions": [
        "alliance mission",
        "cooperative mission",
        "shared call",
        "joint mission",
        "shared callout",
        "shared event"
    ],
    "credits boost": [
        "double credits",
        "2x event",
        "2x credits",
        "credit boost",
        "bonus event",
        "special event"
    ],
    "rank": [
        "promotion",
        "levels",
        "roles",
        "ranking",
        "player level",
        "rank up"
    ],
    "staff": [
        "employees",
        "personnel",
        "workers",
        "crew",
        "hiring",
        "recruitment"
    ],
    "school": [
        "training center",
        "academy",
        "education building",
        "fire academy",
        "police academy",
        "ems school"
    ],
    "mission requirement": [
        "needed vehicles",
        "vehicle requirement",
        "minimum vehicles",
        "mission details",
        "needed units"
    ],
    "building cost": [
        "price",
        "build price",
        "construction cost",
        "costs",
        "buy building"
    ],
    "fuel": [
        "gas station",
        "fuel depot",
        "petrol station",
        "fuel base"
    ],
    "prisoner transport": [
        "prison transport",
        "police transport",
        "jail transport",
        "transporting prisoners",
        "transfer prisoner"
    ],
    "ems": [
        "ambulance",
        "medical",
        "paramedic",
        "medic unit",
        "emergency medical service",
        "rescue medic"
    ],
    "fire": [
        "fire department",
        "firefighting",
        "fire engine",
        "fire station",
        "firehouse",
        "fd"
    ],
    "police": [
        "law enforcement",
        "cop",
        "officer",
        "pd",
        "police car",
        "police department"
    ],
    "wildland": [
        "forest fire",
        "brush fire",
        "wildfire",
        "wildland unit",
        "wildland firefighting"
    ],
    "airport": [
        "airfield",
        "runway",
        "aircraft",
        "plane",
        "hangar",
        "aviation",
        "arff"
    ],
    "quint": [
        "quints",
        "combination truck",
        "ladder pump",
        "engine ladder",
        "platform pumper",
        "quint engine"
    ],
    "rescue engine": [
        "combination rescue",
        "rescue pumper",
        "engine rescue",
        "rescue truck",
        "heavy rescue with pump"
    ],
    "mobile command": [
        "command post",
        "mobile headquarters",
        "command unit",
        "incident command",
        "mobile command center"
    ],
    "hazmat": [
        "hazardous materials",
        "chemical incident",
        "decontamination",
        "hazmat unit",
        "hazmat truck"
    ],
    "sar": [
        "search and rescue",
        "coastal rescue",
        "sea rescue",
        "lifeguard",
        "boat rescue",
        "rescue swimmer"
    ],
    "k9": [
        "police dog",
        "canine unit",
        "dog unit",
        "k-9"
    ],
    "swat": [
        "special weapons and tactics",
        "tactical unit",
        "swat team",
        "swat suv",
        "tactical response"
    ],
    "riot": [
        "riot police",
        "crowd control",
        "riot unit",
        "riot team",
        "riot training"
    ],
    "air unit": [
        "helicopter",
        "air rescue",
        "police helicopter",
        "ems helicopter",
        "air ambulance"
    ],
    "trailer": [
        "utility trailer",
        "special trailer",
        "boat trailer",
        "hazmat trailer"
    ],
    "event": [
        "seasonal event",
        "special event",
        "holiday event",
        "limited time event",
        "event missions"
    ],
    "map": [
        "location",
        "world map",
        "game map",
        "map view",
        "area view"
    ],
    "coastal": [
        "beach",
        "ocean",
        "sea",
        "lifeguard",
        "coastguard"
    ]
}
