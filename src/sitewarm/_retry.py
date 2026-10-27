from __future__ import annotations

import math
import random
import re
import time
from email.utils import parsedate_to_datetime
from typing import Callable, Optional


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[int]:
    """Parses ``Retry-After``: delta-seconds or an HTTP-date. Returns seconds (>= 0) or None."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if re.fullmatch(r"\d+", v):
        return int(v)
    try:
        at = parsedate_to_datetime(v).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0, math.ceil(at - (time.time() if now is None else now)))


def backoff_delay(attempt: int, base: float, maximum: float, rand: Callable[[], float] = random.random) -> float:
    """Exponential backoff with "equal jitter": half fixed, half random. Seconds."""
    ceiling = min(maximum, base * 2**attempt)
    return float(ceiling / 2 + rand() * (ceiling / 2))
