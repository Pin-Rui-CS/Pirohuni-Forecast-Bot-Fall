"""Read one field from many editions of the same report, by code, from one worked example.

A report published again and again (a daily bulletin, a weekly situation report)
states the same field in the same words each time. The model reads ONE edition and
says what the value and the edition's date are there; code learns the words that
come right before each, and reads the same spot in every other edition.

Why: on 2026-11-16 (Q46837, Mexico's daily measles bulletin) the history builder
planned the right table, one row per bulletin, but reading a 60-edition series one
PDF per turn got 6 rows in 14 turns. The total sat in the same sentence in every
bulletin ("de los cuales se han confirmado a sarampión 20,701").

Nothing is guessed. An edition where the words are missing, where they lead to two
different numbers, or where the date cannot be read gives no row and is reported.
The value's quote is the matched text as it stands in that edition, so the table
store's usual check (the quote is on the page) applies to every row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# Words taken from just before the value (or the date) to recognise its place.
ANCHOR_WORDS = 5
# What may stand between the words and before the number: spaces, punctuation
# ("reported: 3,410") and the markup document-to-text conversion adds (markdown
# bold, table cell bars, line breaks inside cells) -- never a letter or a digit,
# so the words must lead straight to the value.
_MARKUP = r"(?:[^\w]|_|<br\s*/?>)"
_SEP = _MARKUP + "+"
_GAP = _MARKUP + "*"
_WORD = re.compile(r"[^\W\d_]+")
# A number as reports write it: 20,701 / 20.701 / 20 701 / 20701 / 3.5 / -2.
_NUMBER = re.compile(r"-?\d{1,3}(?:[,.  ]\d{3})+(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?")

_MONTHS = {}
for _number, _names in enumerate((
        "january enero janeiro janvier januar gennaio jan ene",
        "february febrero fevereiro fevrier février februar febbraio feb fev",
        "march marzo marco março mars marz märz mar",
        "april abril avril aprile apr abr avr",
        "may mayo maio mai maggio",
        "june junio junho juin juni giugno jun",
        "july julio julho juillet juli luglio jul",
        "august agosto aout août aug ago",
        "september septiembre setiembre setembro septembre settembre sep sept set",
        "october octubre outubro octobre oktober ottobre oct out okt ott",
        "november noviembre novembro novembre nov",
        "december diciembre dezembro decembre décembre dezember dicembre dec dic dez"), start=1):
    for _name in _names.split():
        _MONTHS[_name] = _number


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


_TAGS = re.compile(r"<[^<>\n]{1,40}>")


def _clean_view(text: str) -> str:
    """The text with markup removed, for finding dates that conversion split
    across table cells: "12 de noviembre d|e 2026", "07 de septie|<br>mbre".
    A cell break runs the pieces back together; any other tag is a space."""
    text = re.sub(r"\|\s*<br\s*/?>", "", str(text or ""), flags=re.IGNORECASE)
    text = _TAGS.sub(" ", text)
    text = re.sub(r"[*_|]", "", text)
    return squash(text)


def _anchor_pattern(words: list[str]) -> re.Pattern:
    return re.compile(_SEP.join(re.escape(w) for w in words), re.IGNORECASE)


def _value_pattern(words: list[str]) -> re.Pattern:
    """The anchor words, markup allowed between them, then the number."""
    return re.compile(_SEP.join(re.escape(w) for w in words) + _GAP + r"(" + _NUMBER.pattern + r")",
                      re.IGNORECASE)


@dataclass
class Learned:
    value_patterns: list[re.Pattern]
    date_anchor: re.Pattern
    date_shape: re.Pattern
    order: list[str]            # what each number in the date is: "d", "m", "y"
    month_word: bool            # the date names its month in words
    notes: list[str] = field(default_factory=list)


def learn(example_text: str, value: str, date_text: str, date_iso: str) -> tuple[Learned | None, str]:
    """(what was learned, "") or (None, why not) from the example edition."""
    text = squash(example_text)
    want = _digits(value)
    if not want:
        return None, f"the example value {value!r} holds no digits"
    try:
        iso = date.fromisoformat(str(date_iso).strip())
    except ValueError:
        return None, f"the example date's ISO form {date_iso!r} is not YYYY-MM-DD"

    patterns: list[re.Pattern] = []
    for m in _NUMBER.finditer(text):
        if _digits(m.group(0)) != want:
            continue
        before = _TAGS.sub(" ", text[max(0, m.start() - 300):m.start()])
        # The words must lead straight to the value: no other number between.
        # Markup tags are not words: a chart label "<br>n= 20,193" would
        # otherwise teach "br n".
        tail = re.split(r"\d", before)[-1]
        words = _WORD.findall(tail)[-ANCHOR_WORDS:]
        # The fewest words that still lead only to this value: extra words
        # are often layout debris that other editions do not share.
        for size in range(2, len(words) + 1):
            pattern = _value_pattern(words[-size:])
            found = {_digits(x.group(1)) for x in pattern.finditer(text)}
            if found == {want}:
                if pattern.pattern not in {p.pattern for p in patterns}:
                    patterns.append(pattern)
                break
    if not patterns:
        return None, (f"could not find {value!r} after a run of words in the example edition (it may sit "
                      f"only inside a table, between other numbers); give an example where the edition "
                      f"states it in words, or read the editions one by one")

    clean = _clean_view(example_text)
    target = squash(date_text).lower()
    spots = [m.start() for m in re.finditer(re.escape(target), clean.lower())] if target else []
    if not spots:
        return None, f"the example date {date_text!r} was not found in the example edition's text"
    shape_parts, order, month_word = [], [], False
    numbers = {"d": iso.day, "m": iso.month, "y": iso.year}
    names_month = any(_MONTHS.get(w.lower()) == iso.month for w in _WORD.findall(date_text))
    if iso.day == iso.month and not names_month:
        # "11/11/2026" cannot show which number is the day; a guess would
        # misdate every other edition.
        return None, (f"in {date_text!r} the day and the month are the same number, so the date's order "
                      f"cannot be learned; give an example edition whose day and month differ")
    for token in re.findall(r"\d+|[^\W\d_]+|[^\w\s]", date_text.strip()):
        if token.isdigit():
            n = int(token)
            # When the month is written as a word, no number can be the month
            # ("11 de noviembre": the 11 is the day).
            roles = ("y", "d") if names_month else ("y", "m", "d")
            role = next((k for k in roles if k not in order
                         and (numbers[k] == n or (k == "y" and n == iso.year % 100))), "")
            if not role:
                return None, f"could not match {token!r} in {date_text!r} to {date_iso}"
            order.append(role)
            shape_parts.append(r"\d{1,4}")
        elif token.isalpha():
            if _MONTHS.get(token.lower()) == iso.month:
                month_word = True
                shape_parts.append(r"[^\W\d_]{3,12}\.?")
            else:
                shape_parts.append(re.escape(token))
        else:
            shape_parts.append(re.escape(token))
    if "y" not in order or ("m" not in order and not month_word) or "d" not in order:
        return None, f"the example date {date_text!r} needs a day, a month and a year"
    shape = re.compile(r"\s*".join(shape_parts), re.IGNORECASE)
    # The edition's own date must be found after its own words: a report holds
    # other dates too (the outbreak's start, a data cut-off, a next release).
    # A report may state its date more than once (a header and a footer line):
    # use the first place whose preceding words occur nowhere else.
    anchor = None
    for at in spots:
        words = _WORD.findall(re.split(r"\d", clean[max(0, at - 200):at])[-1])[-ANCHOR_WORDS:]
        for size in range(2, len(words) + 1):
            candidate = _anchor_pattern(words[-size:])
            if len(candidate.findall(clean)) == 1:
                anchor = candidate
                break
        if anchor is not None:
            break
    if anchor is None:
        return None, (f"the example date {date_text!r} has no words just before it that occur only there; "
                      f"give the date as the edition states it in a sentence or heading")
    learned = Learned(patterns, anchor, shape, order, month_word)
    probe = read(example_text, learned)
    if probe.value is None or _digits(probe.value) != want or probe.date != iso.isoformat():
        return None, f"the learned reading does not give back the example ({probe.reason or probe.value})"
    return learned, ""


@dataclass
class Reading:
    value: str | None = None
    quote: str = ""
    date: str = ""
    reason: str = ""


def _parse_date(found: str, learned: Learned) -> str:
    numbers = [int(n) for n in re.findall(r"\d+", found)]
    month = None
    if learned.month_word:
        for word in _WORD.findall(found):
            month = _MONTHS.get(word.lower().rstrip("."), month)
        if month is None:
            return ""
    parts = dict(zip(learned.order, numbers))
    if month is not None:
        parts["m"] = month
    if len(parts) < 3:
        return ""
    year = parts["y"] + (2000 if parts["y"] < 100 else 0)
    try:
        return date(year, parts["m"], parts["d"]).isoformat()
    except ValueError:
        return ""


def read(text: str, learned: Learned) -> Reading:
    """The field's value and the edition's date in one edition's text."""
    flat = squash(text)
    # Every place the example stated the value is read, and they must agree:
    # on 2026-09-30 Mexico's bulletin said 20,194 in its sentence and table but
    # "n= 20,193" on a chart, and a single place would have picked one blindly.
    readings: dict[str, tuple[str, str]] = {}
    for pattern in learned.value_patterns:
        for m in pattern.finditer(flat):
            readings.setdefault(_digits(m.group(1)), (m.group(1), m.group(0)))
    if not readings:
        return Reading(reason="the words before the value were not found")
    if len(readings) > 1:
        return Reading(reason="the edition states different values in the places the example used: "
                              + " vs ".join(v for v, _ in readings.values()))
    value, quote = next(iter(readings.values()))
    clean = _clean_view(text)
    spot = learned.date_anchor.search(clean)
    if spot is None:
        return Reading(value=value, quote=quote, reason="the words before the edition's date were not found")
    found_date = learned.date_shape.match(re.sub(r"^[^\w]+", "", clean[spot.end():spot.end() + 80]))
    iso = _parse_date(found_date.group(0), learned) if found_date else ""
    if not iso:
        return Reading(value=value, quote=quote, reason="the edition's date could not be read")
    return Reading(value=value, quote=quote, date=iso)
