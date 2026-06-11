"""
_limiter.py — Rate limiter singleton.

Lives in its own module so both main.py (for the global default limit and
exception handler) and the route modules (for per-endpoint @limiter.limit
decorators) can import the same instance without a circular import.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])


def wallet_or_ip(request) -> str:
    """Rate-limit key: the declared agent wallet, falling back to client IP.

    IP-only keying is useless against one wallet rotating IPs (and unfair to
    many agents behind one CGNAT). The header is self-declared, so a spoofer
    rotating fake wallets just falls back to being capped by the IP-keyed
    limit that runs alongside this one.
    """
    return request.headers.get("x-agent-address") or get_remote_address(request)
