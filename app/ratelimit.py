"""
In-memory sliding-window rate limiter.

Three limit tiers applied per client IP:

  BURST  — very tight, protects expensive endpoints
           POST /api/run     : 5 requests / hour   (each run = Claude API cost)
           POST /api/sync/*  : 20 requests / hour

  NORMAL — general API endpoints
           All /api/* routes : 60 requests / minute

  GLOBAL — page loads and static assets
           All routes        : 300 requests / minute

Requests arriving from TRUSTED_NETWORKS get 10× the limit on every tier
(they've already been authenticated by Authelia; we still track them to catch
runaway scheduled jobs or bugs).

Memory: each IP×rule pair stores a deque of timestamps. A background sweep
runs every 5 minutes to evict windows older than 1 hour.
"""
import logging
import threading
import time
from collections import deque
from typing import Optional

from flask import Flask, Response, request

logger = logging.getLogger(__name__)

# (max_requests, window_seconds)
_TIERS = {
    "burst_run":   (5,   3600),   # /api/run
    "burst_sync":  (20,  3600),   # /api/sync/*
    "normal_api":  (60,  60),     # all /api/*
    "global":      (300, 60),     # everything
}
_TRUSTED_MULTIPLIER = 10          # trusted IPs get 10× every limit


class _SlidingWindow:
    """Thread-safe per-key sliding window counter."""

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._store: dict[tuple, deque] = {}

    def is_allowed(self, key: tuple, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        """
        Returns (allowed, retry_after_seconds).
        retry_after_seconds is 0 when allowed.
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            if key not in self._store:
                self._store[key] = deque()
            dq = self._store[key]

            # Evict timestamps outside the window
            while dq and dq[0] < cutoff:
                dq.popleft()

            if len(dq) >= max_requests:
                # Oldest timestamp tells us when a slot frees up
                retry_after = int(dq[0] - cutoff) + 1
                return False, retry_after

            dq.append(now)
            return True, 0

    def sweep(self, max_age_seconds: int = 3600) -> None:
        """Remove windows that have been idle for max_age_seconds."""
        cutoff = time.monotonic() - max_age_seconds
        with self._lock:
            dead = [k for k, dq in self._store.items() if not dq or dq[-1] < cutoff]
            for k in dead:
                del self._store[k]
        if dead:
            logger.debug("Rate limiter swept %d idle windows", len(dead))


_window = _SlidingWindow()


def _classify(path: str, method: str) -> list[str]:
    """Return the list of tier names that apply to this request."""
    tiers = ["global"]
    if path.startswith("/api/"):
        tiers.append("normal_api")
    if method == "POST" and path in ("/api/run", "/api/discovery/generate"):
        tiers.append("burst_run")
    if method == "POST" and path.startswith("/api/sync"):
        tiers.append("burst_sync")
    return tiers


def setup_rate_limiting(app: Flask, trusted_networks) -> None:
    """
    Attach rate limiting to the Flask app.
    `trusted_networks` is the same list of ip_network objects used by auth.
    """

    def _is_trusted(ip_str: str) -> bool:
        if not trusted_networks:
            return False
        import ipaddress
        try:
            addr = ipaddress.ip_address(ip_str)
            return any(addr in net for net in trusted_networks)
        except ValueError:
            return False

    @app.before_request
    def check_rate_limit():
        ip      = request.remote_addr or "unknown"
        path    = request.path
        method  = request.method
        trusted = _is_trusted(ip)

        for tier in _classify(path, method):
            max_req, window = _TIERS[tier]
            if trusted:
                max_req *= _TRUSTED_MULTIPLIER

            allowed, retry_after = _window.is_allowed(
                key=(ip, tier),
                max_requests=max_req,
                window_seconds=window,
            )
            if not allowed:
                logger.warning(
                    "Rate limit hit: ip=%s tier=%s path=%s", ip, tier, path
                )
                return Response(
                    f"Rate limit exceeded. Retry after {retry_after}s.",
                    status=429,
                    headers={
                        "Retry-After": str(retry_after),
                        "Content-Type": "text/plain",
                    },
                )
        return None

    # Background sweep thread
    def _sweep_loop() -> None:
        while True:
            time.sleep(300)
            _window.sweep()

    t = threading.Thread(target=_sweep_loop, daemon=True, name="ratelimit-sweep")
    t.start()

    logger.info(
        "Rate limiting active — burst_run=%d/h  burst_sync=%d/h  "
        "normal_api=%d/min  global=%d/min  (trusted IPs: %d×)",
        _TIERS["burst_run"][0], _TIERS["burst_sync"][0],
        _TIERS["normal_api"][0], _TIERS["global"][0],
        _TRUSTED_MULTIPLIER,
    )
