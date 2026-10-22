"""Сдвиг субтитров SRT во времени.

Нужен, когда видео начинается не с нуля: при обрезке по тайм-кодам и при
случайном срезе начала. Без сдвига фразы появлялись бы раньше, чем их говорят.
"""
import os
import re
import tempfile

_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_LINE = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)(.*)$")


def _parse(stamp):
    h, m, s, ms = _TIME.match(stamp).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _format(seconds):
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def shift_srt(text: str, offset: float) -> str:
    """Сдвигает все фразы на offset секунд назад (положительный — к началу).

    Фразы, целиком ушедшие до нуля, выбрасываются; начатые раньше нуля
    обрезаются по нулю. Нумерация пересобирается.
    """
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip().lstrip("﻿"))
    out = []
    for block in blocks:
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        match = _LINE.match(lines[timing])
        if not match:
            continue
        try:
            start = _parse(match.group(1)) - offset
            end = _parse(match.group(2)) - offset
        except AttributeError:
            continue
        if end <= 0:
            continue
        start = max(0.0, start)
        body = lines[timing + 1:]
        out.append(f"{len(out) + 1}\n{_format(start)} --> {_format(end)}\n" + "\n".join(body))
    return "\n\n".join(out) + "\n"


def shift_srt_to_temp(path: str, offset: float) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        shifted = shift_srt(f.read(), offset)
    handle, out_path = tempfile.mkstemp(prefix="reels_srt_", suffix=".srt")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        f.write(shifted)
    return out_path
