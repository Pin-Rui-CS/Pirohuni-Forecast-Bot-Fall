"""Facts: lines that state a value, and whether a later text still holds them.

A fact is identified by its set of values (numbers worth following), not by its
wording, because every rewriting step rephrases but should not change a value.
Used by the brief writer's audit pass (compiler.py: which input values the
draft brief lost), and by diagnostics: the run lineage (follow a fact through
the graph) and the brief-loss study (the brief writer's input against the brief
it wrote). It lives outside diagnostics/ because production code depends on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,})(?![\w])")
HOST = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|org|gov|net|edu|io|co|uk|us|int|info|br|de|fr|news|tv))\b", re.I)
LINKISH = re.compile(r"https?://\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}/\S*", re.I)
PCT = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s?%")
LEAD = re.compile(r"^\s*(?:[-*•>|]|\d+[.)]|\[E\d+\])\s*")
_LETTERS = re.compile(r"[^A-Za-zÀ-ÿ ]+")
# Numbers that are metadata, not claims: search relevance scores, labelled
# ("Score: 0.68496037") or bare (0. plus 5+ decimals); clock times ("400 PM CDT",
# "1815 UTC"); and WMO bulletin headers ("WTNT44 KWNH 102038").
_METADATA = re.compile(
    r"\b(?:score|relevance)\s*[:=]\s*\d*\.?\d+"
    r"|\b0\.\d{5,}\b"
    r"|\b\d{3,4}\s?(?:AM|PM|a\.m\.|p\.m\.|UTC|GMT|Z)\b"
    r"|\b[A-Z]{4}\d{2}\s+[A-Z]{4}\s+\d{6}\b",
    re.I,
)


def values(text: str) -> set[str]:
    """Values worth following: grouped or 3+ digit integers, decimals and
    percentages. A bare year is a date, not a value ("2,088" is a value)."""
    out = set()
    for token in NUMBER.findall(text or ""):
        if re.fullmatch(r"(19|20)\d\d", token):
            continue
        clean = token.replace(",", "")
        out.add(clean.rstrip("0").rstrip(".") if "." in clean else clean)
    out.update(m.group(1) + "%" for m in PCT.finditer(text or ""))
    return out


def distinctive(value: str) -> bool:
    """A value unlikely to recur in an unrelated text: a percentage, or at least
    three significant digits ("14.4", "998", "2,088"; not "2.2" or "0.5")."""
    if value.endswith("%"):
        return True
    return len(re.sub(r"\D", "", value).lstrip("0")) >= 3


def _strip(text: str) -> str:
    return _METADATA.sub(" ", LINKISH.sub(" ", text or ""))


def text_values(text: str) -> set[str]:
    """Every value a text holds, ignoring digits inside URLs and paths and
    search-listing scores."""
    return values(_strip(text))


@dataclass(frozen=True)
class Fact:
    claim: str            # the line, lead markers stripped, at most 240 chars
    source: str           # nearest host on or above the line within its block
    key: frozenset        # its values
    line_no: int          # 0-based line within the text it came from
    raw: str              # the full original line


def claims(text: str, *, seen: set | None = None, limit: int | None = None) -> list[Fact]:
    """Value-bearing claim lines of a text, deduplicated by value set.

    A line qualifies with at least two values, or one strong value (a decimal,
    a percentage, or 4+ digits), and some words: a bare value ("Score: 0.87")
    is not a claim. The source is the nearest host on the line or above it,
    reset at each markdown heading. Pass `seen` to deduplicate across texts.
    """
    seen = set() if seen is None else seen
    out: list[Fact] = []
    source = ""
    for line_no, line in enumerate((text or "").splitlines()):
        if line.lstrip().startswith("#"):
            source = ""
        host = HOST.search(line)
        if host:
            source = host.group(1).lower().removeprefix("www.")
        stripped = _strip(line)
        nums = values(stripped)
        if not nums or (len(nums) < 2 and not any(distinctive(n) for n in nums)) \
                or len(_LETTERS.sub(" ", stripped).replace(" ", "")) < 15:
            continue
        key = frozenset(nums)
        if key in seen:
            continue
        seen.add(key)
        out.append(Fact(LEAD.sub("", line).strip()[:240], source, key, line_no, line))
        if limit is not None and len(out) >= limit:
            break
    return out


def presence(key: frozenset, held: set) -> str:
    """'present' (every value held), 'partial' (some) or 'absent' (none)."""
    found = len(key & held)
    if found == len(key):
        return "present"
    return "partial" if found else "absent"


_EXCLUDED_LINE = re.compile(r"items EXCLUDED", re.I)


def split_excluded(brief: str) -> tuple[str, str]:
    """(brief without its 'Decision-relevant items EXCLUDED' passage, the passage).

    The brief writer lists what it deliberately left out in its Balance Check.
    A value that appears only there was dropped WITH a stated reason, which is
    different from a silent drop. The passage runs from that line to the next
    heading or top-level bullet.
    """
    lines = (brief or "").splitlines()
    start = next((i for i, line in enumerate(lines) if _EXCLUDED_LINE.search(line)), None)
    if start is None:
        return brief or "", ""
    end = start + 1
    while end < len(lines) and not re.match(r"^(#{1,6} |- |\* |\[E\d+\])", lines[end]):
        end += 1
    passage = "\n".join(lines[start:end])
    return "\n".join(lines[:start] + lines[end:]), passage


KEPT, CHANGED, DROPPED_WITH_REASON, DROPPED_SILENTLY = "kept", "changed", "dropped_with_reason", "dropped_silently"


def brief_state(key: frozenset, body: set, excluded: set, *, source: str = "", excluded_text: str = "") -> str:
    """What a brief did with a fact, given the values of its body and of its
    'items EXCLUDED' passage: kept (all values in the body), changed (some),
    dropped with a reason (its values, or its source, named in the excluded
    passage — which lists items mostly by source and URL), or dropped silently."""
    held = presence(key, body)
    if held == "present":
        return KEPT
    if held == "partial":
        return CHANGED
    if key <= excluded or (source and source in (excluded_text or "").lower()):
        return DROPPED_WITH_REASON
    return DROPPED_SILENTLY
