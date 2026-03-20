"""
SolarEdge Optimizers Integration - Custom exceptions.

Defines exception types used by the integration so callers can catch specific
errors instead of generic Exception. Supports clearer error handling and Pylint/CodeFactor-friendly code paths.
"""


class SolarEdgeAPIError(Exception):
    """Raised when the SolarEdge API returns an error or data cannot be processed."""
