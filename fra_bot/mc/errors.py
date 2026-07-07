"""Errors raised by the MissionChief client."""

from __future__ import annotations


class MissionChiefError(RuntimeError):
    """Base class for MissionChief client errors."""


class LoginError(MissionChiefError):
    """Login failed (bad credentials or unexpected page layout)."""


class SessionExpiredError(MissionChiefError):
    """We were redirected to the sign-in page mid-scrape."""


class FetchError(MissionChiefError):
    """A page could not be fetched after retries."""

    def __init__(self, url: str, status: int | None = None, message: str | None = None):
        self.url = url
        self.status = status
        super().__init__(message or f"Failed to fetch {url} (status={status})")


class ParseError(MissionChiefError):
    """A page was fetched but its HTML did not match what we expect."""
