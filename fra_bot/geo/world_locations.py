"""A worldwide pool of real, inhabited places for the daily auto-build.

The alliance plays worldwide, so the daily hospital/prison build picks a
random real city from every populated continent (not a US-only list). The
chosen name is geocoded to real coordinates at build time; a small random
jitter spreads buildings around the city instead of stacking them on the
exact centre.
"""

from __future__ import annotations

import math
import random

# A broad, deliberately global spread. Each entry is unambiguous enough for
# the geocoder ("City, Country"). Not exhaustive — just varied enough that the
# daily build roams the whole map rather than one region.
WORLD_CITIES: tuple[str, ...] = (
    # North America
    "New York City, USA", "Los Angeles, USA", "Chicago, USA", "Houston, USA",
    "Toronto, Canada", "Vancouver, Canada", "Montreal, Canada",
    "Mexico City, Mexico", "Guadalajara, Mexico", "Monterrey, Mexico",
    "Havana, Cuba", "Panama City, Panama", "San José, Costa Rica",
    # South America
    "São Paulo, Brazil", "Rio de Janeiro, Brazil", "Brasília, Brazil",
    "Buenos Aires, Argentina", "Córdoba, Argentina", "Santiago, Chile",
    "Lima, Peru", "Bogotá, Colombia", "Medellín, Colombia",
    "Caracas, Venezuela", "Quito, Ecuador", "Montevideo, Uruguay",
    "La Paz, Bolivia", "Asunción, Paraguay",
    # Europe
    "London, United Kingdom", "Manchester, United Kingdom", "Dublin, Ireland",
    "Paris, France", "Marseille, France", "Madrid, Spain", "Barcelona, Spain",
    "Lisbon, Portugal", "Amsterdam, Netherlands", "Rotterdam, Netherlands",
    "Brussels, Belgium", "Berlin, Germany", "Munich, Germany",
    "Hamburg, Germany", "Frankfurt, Germany", "Zurich, Switzerland",
    "Vienna, Austria", "Rome, Italy", "Milan, Italy", "Naples, Italy",
    "Copenhagen, Denmark", "Stockholm, Sweden", "Oslo, Norway",
    "Helsinki, Finland", "Warsaw, Poland", "Kraków, Poland", "Prague, Czechia",
    "Budapest, Hungary", "Bucharest, Romania", "Athens, Greece",
    "Kyiv, Ukraine", "Belgrade, Serbia", "Zagreb, Croatia",
    # Africa
    "Cairo, Egypt", "Alexandria, Egypt", "Lagos, Nigeria", "Abuja, Nigeria",
    "Nairobi, Kenya", "Mombasa, Kenya", "Accra, Ghana", "Dakar, Senegal",
    "Casablanca, Morocco", "Marrakesh, Morocco", "Tunis, Tunisia",
    "Algiers, Algeria", "Addis Ababa, Ethiopia", "Kampala, Uganda",
    "Dar es Salaam, Tanzania", "Cape Town, South Africa",
    "Johannesburg, South Africa", "Durban, South Africa", "Luanda, Angola",
    "Kinshasa, DR Congo",
    # Middle East
    "Istanbul, Turkey", "Ankara, Turkey", "Tel Aviv, Israel",
    "Dubai, United Arab Emirates", "Abu Dhabi, United Arab Emirates",
    "Doha, Qatar", "Riyadh, Saudi Arabia", "Jeddah, Saudi Arabia",
    "Amman, Jordan", "Beirut, Lebanon", "Kuwait City, Kuwait",
    # Asia
    "Tokyo, Japan", "Osaka, Japan", "Seoul, South Korea", "Busan, South Korea",
    "Beijing, China", "Shanghai, China", "Guangzhou, China",
    "Hong Kong, China", "Taipei, Taiwan", "Delhi, India", "Mumbai, India",
    "Bengaluru, India", "Chennai, India", "Kolkata, India",
    "Karachi, Pakistan", "Lahore, Pakistan", "Dhaka, Bangladesh",
    "Bangkok, Thailand", "Hanoi, Vietnam", "Ho Chi Minh City, Vietnam",
    "Jakarta, Indonesia", "Surabaya, Indonesia", "Kuala Lumpur, Malaysia",
    "Singapore, Singapore", "Manila, Philippines", "Cebu City, Philippines",
    "Almaty, Kazakhstan", "Tashkent, Uzbekistan",
    # Oceania
    "Sydney, Australia", "Melbourne, Australia", "Brisbane, Australia",
    "Perth, Australia", "Adelaide, Australia", "Auckland, New Zealand",
    "Wellington, New Zealand", "Suva, Fiji",
)


def random_world_location(rng: random.Random | None = None) -> str:
    """A random real city from the worldwide pool (geocode it for coords)."""
    return (rng or random).choice(WORLD_CITIES)


def random_world_locations(n: int, rng: random.Random | None = None) -> list[str]:
    """``n`` distinct random cities (falls back to repeats if n exceeds the pool)."""
    r = rng or random
    if n <= len(WORLD_CITIES):
        return r.sample(WORLD_CITIES, n)
    return [r.choice(WORLD_CITIES) for _ in range(n)]


def jitter_coords(
    latitude: float,
    longitude: float,
    *,
    max_km: float = 8.0,
    rng: random.Random | None = None,
) -> tuple[float, float]:
    """Nudge a point by up to ``max_km`` in a random direction so repeated
    builds near the same city don't land on the exact same spot."""
    r = rng or random
    d_lat = (r.uniform(-1.0, 1.0) * max_km) / 111.0
    cos_lat = max(0.2, math.cos(math.radians(latitude)))
    d_lng = (r.uniform(-1.0, 1.0) * max_km) / (111.0 * cos_lat)
    return latitude + d_lat, longitude + d_lng
