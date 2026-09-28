"""Title numbers stated in a listing's own text, e.g. "registered under title numbers SY123456 and SY654321".

Strict on purpose: a number counts only right after a phrase that names it a title number, and only as part of an
unbroken list ("A, B and C"). Postcodes, grid references and lot numbers look alike (TQ123456 is both an OS grid
reference and a valid title number), so a bare letters-plus-digits string is never taken.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from bessible.models import TITLE_NUMBER_RE

# The registry's format (letters then digits) is not enough on its own: a number needs the context below.
NUMBER = TITLE_NUMBER_RE
_CONTEXT = re.compile(
    r"(?i:\b(?:registered\s+(?:under|with)\s+)?titles?(?:\s+(?:numbers?|nos?\.?|references?|refs?\.?))?)"
    r"\s*(?:is|are|:|-)?\s*"
)
_FIRST = re.compile(rf"({NUMBER})\b")
_NEXT = re.compile(rf"\s*(?:,|;|/|&|\band\b|\bor\b)\s*(?:(?i:title\s+)?)({NUMBER})\b")


class ListingTitleNumber(BaseModel):
    """A title number and the sentence that states it."""

    title_number: str
    evidence: str


def _sentence_around(text: str, start: int, end: int) -> str:
    before = text[:start]
    head = before[before.rfind(".") + 1 :] if "." in before else before
    head = head[head.rfind("\n") + 1 :]
    tail = text[end:]
    stop = min((i for i in (tail.find("."), tail.find("\n")) if i >= 0), default=len(tail))
    return re.sub(r"\s+", " ", (head + text[start:end] + tail[:stop])).strip()


def find_title_numbers(text: str) -> list[ListingTitleNumber]:
    """Every title number the text states as one, once each, in order of first mention."""
    found: dict[str, ListingTitleNumber] = {}
    for ctx in _CONTEXT.finditer(text):
        pos = ctx.end()
        first = _FIRST.match(text, pos)
        if not first:
            continue
        numbers = [first.group(1)]
        pos = first.end()
        while nxt := _NEXT.match(text, pos):
            numbers.append(nxt.group(1))
            pos = nxt.end()
        evidence = _sentence_around(text, ctx.start(), pos)
        for n in numbers:
            found.setdefault(n, ListingTitleNumber(title_number=n, evidence=evidence))
    return list(found.values())
