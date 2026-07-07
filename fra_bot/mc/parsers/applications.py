"""Parser for alliance applications (/verband/bewerbungen).

Rows are identified by their accept link — the only stable, verified
selector: ``/verband/bewerbungen/annehmen/<application_id>``. The
applicant's profile link (``/profile/<id>``) provides the MC user id.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from .common import extract_user_id

ACCEPT_PATH = "/verband/bewerbungen/annehmen/"


def parse_applications_page(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    applications: list[dict[str, Any]] = []
    seen: set[int] = set()

    for row in soup.find_all("tr"):
        accept_link = row.find("a", href=lambda h: h and ACCEPT_PATH in h)
        if accept_link is None:
            continue
        try:
            application_id = int(
                accept_link["href"].split(ACCEPT_PATH, 1)[1].split("?")[0].strip("/")
            )
        except (ValueError, IndexError):
            continue
        if application_id in seen:
            continue
        seen.add(application_id)

        profile_link = row.find("a", href=lambda h: h and "/profile/" in h)
        if profile_link is not None:
            name = profile_link.get_text(strip=True)
            mc_user_id = extract_user_id(profile_link.get("href"))
        else:
            # Fall back to the first non-action link's text.
            name_link = row.find(
                "a", href=lambda h: h and ACCEPT_PATH not in h and "ablehnen" not in h
            )
            name = name_link.get_text(strip=True) if name_link else "Unknown"
            mc_user_id = None

        applications.append(
            {
                "application_id": application_id,
                "applicant_name": name or "Unknown",
                "mc_user_id": mc_user_id,
            }
        )

    return applications
