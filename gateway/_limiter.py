"""
_limiter.py — Rate limiter singleton.

Lives in its own module so both main.py (for the global default limit and
exception handler) and the route modules (for per-endpoint @limiter.limit
decorators) can import the same instance without a circular import.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
