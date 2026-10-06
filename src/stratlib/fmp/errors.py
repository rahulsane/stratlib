"""Errors raised by the FMP client."""

from __future__ import annotations


class FMPError(RuntimeError):
    def __init__(self, message: str, *, path: str | None = None, status: int | None = None):
        super().__init__(message)
        self.path = path
        self.status = status


class FMPAuthError(FMPError):
    """401/403: the API key is missing or invalid."""


class FMPPlanRestrictedError(FMPError):
    """402: the endpoint, parameter value, or symbol is not in the current plan."""


class FMPRequestError(FMPError):
    """Any other 4xx, or a 200 response carrying an error message."""


class FMPServerError(FMPError):
    """429, 5xx, or network failures that persisted through every retry."""
