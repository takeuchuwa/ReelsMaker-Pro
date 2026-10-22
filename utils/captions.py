"""Субтитры как слова со временем: автозамены, сдвиг и сборка файла ASS.

SRT умеет только строку целиком, поэтому для появления слов и подсветки
текущего слова собираем ASS — тот же фильтр subtitles в ffmpeg его рисует.
Обычные строки тоже идут через ASS: один путь для всех режимов.
"""
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

MODE_STATIC = "static"
MODE_APPEAR = "appear"
MODE_HIGHLIGHT = "highlight"

# Пауза дольше этой — новая строка, даже если слов в ней ещё мало: иначе
# строка висела бы на экране, пока диктор молчит.
LINE_BREAK_GAP = 1.0
LINE_HOLD = 0.4

_PUNCT = "\"'«»„“”.,!?…:;()[]-–—"


@dataclass
class Word:
    text: str
    start: float
    end: float


Line = List[Word]


# --- источники ------------------------------------------------------------
def load_whisper_words(path: str, origin: float = 0.0) -> List[Word]:
    """Слова из распознавания; origin переводит их во время исходника."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    words = []
    for item in data.get("words", []):
        text = str(item.get("word", "")).strip()
        if text:
            words.append(Word(text, float(item["start"]) + origin, float(item["end"]) + origin))
    return words


def group_words(words: List[Word], per_line: int) -> List[Line]:
    per_line = max(1, int(per_line or 1))
    lines, current = [], []
    for word in words:
        if current and (len(current) >= per_line or word.start - current[-1].end > LINE_BREAK_GAP):
            lines.append(current)
            current = []
        current.append(word)
    if current:
        lines.append(current)
    return lines


_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _srt_seconds(stamp: str) -> float:
    h, m, s, ms = _SRT_TIME.match(stamp.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def load_srt_lines(path: str) -> List[Line]:
    """Строки готового SRT. Времени отдельных слов в нём нет, поэтому оно
    раскладывается по длине слов — для анимации это приближение."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    lines = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        rows = block.splitlines()
        timing = next((i for i, row in enumerate(rows) if "-->" in row), None)
        if timing is None:
            continue
        try:
            left, right = rows[timing].split("-->")
            start, end = _srt_seconds(left), _srt_seconds(right.split()[0])
        except (ValueError, AttributeError, IndexError):
            continue
        tokens = " ".join(rows[timing + 1:]).split()
        tokens = [re.sub(r"<[^>]+>", "", t) for t in tokens]
        tokens = [t for t in tokens if t]
        if not tokens or end <= start:
            continue
        weights = [len(t.strip(_PUNCT)) + 1 for t in tokens]
        total, at, line = sum(weights), start, []
        for token, weight in zip(tokens, weights):
            span = (end - start) * weight / total
            line.append(Word(token, at, at + span))
            at += span
        lines.append(line)
    return lines


# --- автозамены -----------------------------------------------------------
def parse_fixes(text: str) -> List[Tuple[List[str], str]]:
    """Строки «было = стало» (или «было -> стало»). Регистр «было» не важен."""
    rules = []
    for row in (text or "").splitlines():
        row = row.strip()
        if not row or row.startswith("#"):
            continue
        parts = re.split(r"\s*(?:->|→|=)\s*", row, maxsplit=1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        source = [_norm(t) for t in parts[0].split()]
        if all(source):
            rules.append((source, parts[1].strip()))
    # Длинные фразы раньше коротких: «олег позняков» не должно разбиться
    # правилом для одного «позняков».
    rules.sort(key=lambda rule: -len(rule[0]))
    return rules


def _norm(token: str) -> str:
    return token.strip(_PUNCT).lower().replace("ё", "е")


def _split_punct(token: str):
    core = token.strip(_PUNCT)
    if not core:
        return token, "", ""
    head = token[:token.find(core)]
    tail = token[token.find(core) + len(core):]
    return head, core, tail


def _apply_single(word: Word, source: str, target: str) -> Optional[Word]:
    head, core, tail = _split_punct(word.text)
    lowered = core.lower().replace("ё", "е")
    if lowered == source:
        return replace(word, text=head + target + tail)
    # Падежные окончания: «Познякова» по правилу «позняков» становится
    # «Позднякова». Окончание короткое, иначе задевали бы другие слова.
    if len(source) >= 4 and lowered.startswith(source) and 0 < len(lowered) - len(source) <= 3:
        ending = core[len(source):]
        return replace(word, text=head + target + ending + tail)
    return None


def apply_fixes(lines: List[Line], rules) -> List[Line]:
    if not rules:
        return lines
    result = []
    for line in lines:
        out, i = [], 0
        while i < len(line):
            for source, target in rules:
                n = len(source)
                if n == 1:
                    fixed = _apply_single(line[i], source[0], target)
                    if fixed:
                        out.append(fixed)
                        i += 1
                        break
                elif [_norm(w.text) for w in line[i:i + n]] == source:
                    head = _split_punct(line[i].text)[0]
                    tail = _split_punct(line[i + n - 1].text)[2]
                    out.append(Word(head + target + tail, line[i].start, line[i + n - 1].end))
                    i += n
                    break
            else:
                out.append(line[i])
                i += 1
        result.append(out)
    return result


# --- время ----------------------------------------------------------------
def shift_lines(lines: List[Line], offset: float, limit: Optional[float] = None) -> List[Line]:
    """Сдвиг к началу результата: offset — какая секунда исходника стала нулём.
    Слова до нуля и после limit выбрасываются."""
    shifted = []
    for line in lines:
        words = []
        for word in line:
            start, end = word.start - offset, word.end - offset
            if end <= 0 or (limit is not None and start >= limit):
                continue
            words.append(Word(word.text, max(0.0, start), end))
        if words:
            shifted.append(words)
    return shifted


# --- ASS ------------------------------------------------------------------
def _ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(lines: List[Line], style: dict, mode: str) -> str:
    from .ffmpeg_utils import ass_color  # здесь, чтобы не было кругового импорта
    primary = ass_color(style.get("color"), "#FFFFFF")
    outline = ass_color(style.get("outline_color"), "#000000")
    highlight = ass_color(style.get("highlight_color"), "#FFD400")
    size = style.get("font_size", 12)
    font = (style.get("font") or "Arial").replace(",", " ")
    # 384x288 — то же поле, в которое ffmpeg переводит SRT: размеры и отступы
    # значат ровно то же, что и раньше.
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},{primary},{outline},&H80000000,"
        "0,0,0,0,100,100,0,0,1,2,1,2,25,25,70,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []

    def add(start, end, text):
        if end - start >= 0.01:
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")

    for index, line in enumerate(lines):
        next_start = lines[index + 1][0].start if index + 1 < len(lines) else None
        line_end = line[-1].end + LINE_HOLD
        if next_start is not None:
            line_end = max(line[-1].end, min(line_end, next_start))
        texts = [_ass_text(w.text) for w in line]

        if mode == MODE_STATIC:
            add(line[0].start, line_end, " ".join(texts))
            continue

        for i, word in enumerate(line):
            start = word.start
            end = line[i + 1].start if i + 1 < len(line) else line_end
            if mode == MODE_APPEAR:
                # Несказанные слова стоят на своих местах, но прозрачные:
                # строка не прыгает, когда в неё добавляется слово.
                shown = " ".join(texts[:i + 1])
                hidden = " ".join(texts[i + 1:])
                text = shown + (r" {\alpha&HFF&}" + hidden if hidden else "")
            else:
                parts = [(r"{\1c" + highlight + "&}" + t + r"{\r}") if j == i else t
                         for j, t in enumerate(texts)]
                text = " ".join(parts)
            add(start, end, text)
    return header + "\n".join(events) + "\n"


PREVIEW_TEXT = "Так виглядатимуть ваші субтитри"


def build_preview_ass(style: dict, text: str = PREVIEW_TEXT) -> str:
    """Одна строка-образец на весь кадр предпросмотра. В режиме подсветки
    выделено второе слово — иначе цвет выделения было бы не увидеть."""
    from .ffmpeg_utils import ass_color
    texts = [_ass_text(w) for w in text.split()]
    if style.get("mode") == MODE_HIGHLIGHT and len(texts) > 1:
        highlight = ass_color(style.get("highlight_color"), "#FFD400")
        texts[1] = r"{\1c" + highlight + "&}" + texts[1] + r"{\r}"
    header = build_ass([], style, MODE_STATIC)
    return header + f"Dialogue: 0,0:00:00.00,9:00:00.00,Default,,0,0,0,,{' '.join(texts)}\n"


def write_text_temp(content: str, suffix: str = ".ass") -> str:
    handle, path = tempfile.mkstemp(prefix="reels_subs_", suffix=suffix)
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def write_ass_temp(lines: List[Line], style: dict, mode: str) -> str:
    return write_text_temp(build_ass(lines, style, mode))
