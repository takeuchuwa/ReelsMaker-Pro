"""Тайм-коды: разбор того, что человек ввёл руками, и единый вид на экране."""
import re
from typing import Optional

_PATTERN = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d*)?)\s*$")


def parse_timecode(text: str) -> Optional[float]:
    """«83.5», «1:23.5» и «0:01:23,5» — одно и то же время.

    Пустая строка и мусор дают None: вызывающий сам решает, ошибка это или
    «до конца видео».
    """
    match = _PATTERN.match(text or "")
    if not match:
        return None
    first, second, seconds = match.groups()
    if second is not None:
        hours, minutes = int(first), int(second)
    elif first is not None:
        hours, minutes = 0, int(first)
    else:
        hours, minutes = 0, 0
    value = float(seconds.replace(",", "."))
    if (first is not None or second is not None) and value >= 60:
        return None
    return hours * 3600 + minutes * 60 + value


def format_timecode(seconds: Optional[float], with_ms: bool = True) -> str:
    if seconds is None:
        return ""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    tail = f".{ms:03}" if with_ms else ""
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}{tail}"
    return f"{minutes:02}:{secs:02}{tail}"
