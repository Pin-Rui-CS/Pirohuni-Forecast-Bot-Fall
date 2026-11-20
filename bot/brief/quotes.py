"""Key Evidence items hold only text copied from the research (2026-11-20).

The brief writer (a SoCLaaS model) used to write each item in its own words
and attach its own reading of what the fact meant. On Q46883 it labelled a
reported secondary-market price for Anthropic "near-proxy -- direction of the
next mark", put it under "strongest evidence FOR", and wrote a "Derived
Implication" that a mark-up could add billions to Salesforce's quarter. None of
that was in any source; the forecasters built 45-60% of their forecast on it.

So the brief writer now selects and quotes, and code keeps only what it can
find: every quoted passage of an item must be in the research the writer was
given (ignoring case, spacing, markdown emphasis and quote-mark style). An
item is rebuilt from its parts -- tag, tier (one of four words), the quotes
that were found, a fixed date note, and the source -- so no wording of the
writer's own survives. An item with no quote found is removed, and the brief
says how many were. The sections that were only interpretation (the FOR /
AGAINST lines and Derived Implications) are removed if the writer still
writes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

TIERS = ("direct", "adjacent-metric", "near-proxy", "market")
# Notes the writer may add outside the quotes: fixed wording only.
ALLOWED_NOTES = ("(date unverified)", "(year not stated in source)", "(position vs. baseline unverified)")
MIN_QUOTE_CHARS = 15   # a whole sentence is asked for; a bare "May 2026" says nothing checkable
# A quote may skip words with an ellipsis; each piece must then be found.
_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…)\s*")
_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”")
_ITEM = re.compile(r"^(\s*[-*]?\s*)\[E(\d+)\]\s*(.*)$")
_TIER = re.compile(r"^\(([^)]*)\)\s*")
_DOC_TAGS = re.compile(r"(\s*\[D\d+\])+\s*$")
_SOURCE_SPLIT = re.compile(r"\s+[—–]\s+(?=[^\"“”]*$)")   # the last " — " outside any quote
_DROP_SECTIONS = ("## Derived Implications",)
_FOR_AGAINST = re.compile(r"^\s*-\s*Strongest evidence (FOR|AGAINST)\b", re.IGNORECASE)


def _squash(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"[‘’]", "'", text)
    text = re.sub(r"[“”]", '"', text)
    text = re.sub(r"[–—]", "-", text)
    return " ".join(text.split())


@dataclass
class QuoteCheck:
    kept: int = 0
    removed: list[str] = field(default_factory=list)       # items with no quote found
    quotes_dropped: list[str] = field(default_factory=list)  # quotes not found, from kept items
    wording_dropped: int = 0                                # items whose own wording was cut


def _found(quote: str, corpus: str) -> bool:
    pieces = [p for p in _ELLIPSIS.split(quote) if p.strip()]
    if not pieces or sum(len(p) for p in pieces) < MIN_QUOTE_CHARS:
        return False
    return all(_squash(p) in corpus for p in pieces)


_TIER_WORD = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in TIERS) + r")(?![\w-])")


def _tier(raw: str) -> str:
    """The first tier word written, as a whole word ("direction" is not "direct")."""
    match = _TIER_WORD.search(raw.lower())
    return match.group(1) if match else ""


def check_item(line: str, corpus: str, check: QuoteCheck) -> str | None:
    """The item rebuilt from what code can stand behind, or None to remove it."""
    match = _ITEM.match(line)
    if not match:
        return line
    lead, number, rest = match.groups()
    tier_match = _TIER.match(rest)
    tier = _tier(tier_match.group(1)) if tier_match else ""
    rest = rest[tier_match.end():] if tier_match else rest
    tags = _DOC_TAGS.search(rest)
    doc_tags = " ".join(re.findall(r"\[D\d+\]", tags.group(0))) if tags else ""
    rest = rest[:tags.start()] if tags else rest
    # The source is what follows the LAST dash outside any quote; anything the
    # writer put between the quotes and the source is its own wording.
    dashes = [m for m in _SOURCE_SPLIT.finditer(rest)]
    body, source = (rest[:dashes[-1].start()], rest[dashes[-1].end():].strip()) if dashes else (rest, "")
    quotes = [a or b for a, b in _QUOTED.findall(body)]
    good = [q.strip() for q in quotes if _found(q, corpus)]
    check.quotes_dropped.extend(q for q in quotes if q.strip() not in good)
    if not good:
        check.removed.append(line.strip())
        return None
    notes = [n for n in ALLOWED_NOTES if n in body]
    leftover = _QUOTED.sub("", body)
    for note in notes:
        leftover = leftover.replace(note, "")
    if re.search(r"[A-Za-z]{3,}", leftover):
        check.wording_dropped += 1
    check.kept += 1
    text = " … ".join(f'"{q}"' for q in good)
    return (f"{lead}[E{number}]" + (f" ({tier})" if tier else "") + f" {text}"
            + "".join(f" {n}" for n in notes) + (f" — {source}" if source else "")
            + (f" {doc_tags}" if doc_tags else ""))


def enforce(brief: str, corpus: str) -> tuple[str, QuoteCheck]:
    """The brief with every Key Evidence item rebuilt from quotes found in
    ``corpus`` (the research the writer was given), and the interpretation-only
    sections removed."""
    squashed = _squash(corpus)
    check = QuoteCheck()
    out: list[str] = []
    section = ""
    skipping = False
    for line in (brief or "").splitlines():
        if line.startswith("## "):
            section = line.strip()
            skipping = any(section.startswith(s) for s in _DROP_SECTIONS)
            if skipping:
                continue
        if skipping:
            continue
        if _FOR_AGAINST.match(line):
            continue
        if section.startswith("## Key Evidence") and not line.startswith("## "):
            if _ITEM.match(line):
                kept = check_item(line, squashed, check)
                if kept is not None:
                    out.append(kept)
            elif line.strip():
                check.wording_dropped += 1  # the writer's own text between or under items
            else:
                out.append(line)
            continue
        out.append(line)
    text = "\n".join(out)
    if check.removed:
        note = (f"_Code removed {len(check.removed)} evidence item(s): no passage they quoted was found in the "
                "research word for word._")
        text = _after_section(text, "## Key Evidence", note)
    return text, check


def _after_section(text: str, heading: str, note: str) -> str:
    """Put ``note`` at the end of the section headed ``heading``."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().startswith(heading)), None)
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + ["", note] + lines[end:])
