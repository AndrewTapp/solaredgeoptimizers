"""
SolarEdge Optimizers Integration - Custom exceptions.

Defines exception types used by the integration so callers can catch specific
errors instead of generic Exception. Supports clearer error handling and Pylint/CodeFactor-friendly code paths.

SolarEdgeAuthError (v2.4.20+): raised by the dual API verify_authentication() and propagated
from coordinator polling when credentials are rejected; mapped to ConfigEntryAuthFailed in HA.
"""


class SolarEdgeAPIError(Exception):
    """Raised when the SolarEdge API returns an error or data cannot be processed."""


class SolarEdgeAuthError(SolarEdgeAPIError):
    """Raised when SolarEdge credentials are invalid, expired, or rejected."""
