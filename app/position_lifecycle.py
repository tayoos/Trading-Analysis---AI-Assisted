"""
Shared rules for open vs closed positions (dust, archive).
"""
from __future__ import annotations

# Below this share count we treat the position as flat (AVCO rounding / T212 dust).
MIN_OPEN_SHARES = 0.05


def is_open_position(shares: float | None) -> bool:
    return float(shares or 0) > MIN_OPEN_SHARES
