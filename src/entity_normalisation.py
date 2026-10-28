"""Entity-name normalisation: count businesses, not spellings.

What this demonstrates:
- a deterministic, auditable rewrite (case, punctuation, dotted acronyms, legal
  forms) after which matching is plain equality, with no fuzzy score inside it;
- a separate `stem` key for the different question "same name, any legal form";
- a ZIP5 blocking key that keeps every comparison bounded;
- a rule-set version stored with every derived row, so a rule change can be
  backfilled instead of silently mixing two rule sets.

Excerpt from a private production codebase. The production module also
normalises street addresses; that part is omitted and marked
"# STUB: production implementation is private". The doctests run as-is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NORMALIZATION_VERSION = 1
"""Bumped whenever a table below changes, for the reason `chunk_version` exists.

A rewrite rule change silently invalidates every stored normalised value, and
rows normalised under two versions group *almost* correctly - which is worse
than not grouping at all, because the failure is a slightly wrong count rather
than an error. Each derived row records which rules produced it, and the
backfill re-does anything older than this.
"""

# Street types, directionals, unit designators and PO boxes for addresses.
# STUB: production implementation is private

ENTITY_SUFFIXES: dict[str, str] = {
    "LLC": "LLC", "LC": "LLC",
    "LIMITED LIABILITY COMPANY": "LLC",
    "LIMITED LIABILITY CO": "LLC",
    "INC": "INC", "INCORPORATED": "INC",
    "CORP": "CORP", "CORPORATION": "CORP",
    "CO": "CO", "COMPANY": "CO",
    "LTD": "LTD", "LIMITED": "LTD",
    # STUB: production implementation is private
}
"""Legal-form tokens, mapped to one spelling each.

Multi-word keys are matched before single-word ones, so `LIMITED LIABILITY
COMPANY` does not first become `LTD` by way of `LIMITED`. See `_fold_suffixes`.
"""

_DOTTED = {
    ("L", "L", "C"): "LLC",
    ("L", "C"): "LLC",
    # STUB: production implementation is private
}
"""`L.L.C.` survives punctuation-stripping as three tokens and has to be rejoined.

`ACME WIDGETS L.L.C.` becomes `ACME WIDGETS L L C` once full stops become
spaces, and without this it never reaches `ACME WIDGETS LLC`.
"""

_APOSTROPHE = re.compile(r"[’'`]")
_PUNCTUATION = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True, slots=True)
class NormalizedName:
    raw: str
    norm: str
    """Uppercased, depunctuated, legal form folded to one spelling."""

    stem: str
    """`norm` with trailing legal-form tokens removed.

    `ACME WIDGETS LLC` and `ACME WIDGETS INC` share this and share nothing
    else. Never empty: a name that is nothing but a legal form keeps its
    `norm`, because a stem of `""` would group every such row into one entity.
    """

    @property
    def empty(self) -> bool:
        return not self.norm


def _tokens(value: str) -> list[str]:
    """Uppercase, drop apostrophes, turn other punctuation into a break.

    Apostrophes close up (`O'NEIL'S` -> `ONEILS`) and everything else opens
    out (`NORTH-STAR` -> `NORTH STAR`). They are different because an
    apostrophe is inside a word and a hyphen is between two.
    """
    text = value.upper()
    text = _APOSTROPHE.sub("", text)
    text = text.replace("&", " AND ")
    text = _PUNCTUATION.sub(" ", text)
    return text.split()


def _rejoin_dotted(tokens: list[str]) -> list[str]:
    """`L L C` -> `LLC`, longest run first."""
    out: list[str] = []
    index = 0
    while index < len(tokens):
        matched = False
        for size in (3, 2):
            window = tuple(tokens[index : index + size])
            if len(window) == size and window in _DOTTED:
                out.append(_DOTTED[window])
                index += size
                matched = True
                break
        if not matched:
            out.append(tokens[index])
            index += 1
    return out


def _fold_suffixes(tokens: list[str]) -> list[str]:
    """Map every legal-form token to one spelling, longest phrase first."""
    out: list[str] = []
    index = 0
    while index < len(tokens):
        matched = False
        for size in (4, 3, 2):
            phrase = " ".join(tokens[index : index + size])
            if len(tokens[index : index + size]) == size and phrase in ENTITY_SUFFIXES:
                out.append(ENTITY_SUFFIXES[phrase])
                index += size
                matched = True
                break
        if not matched:
            token = tokens[index]
            out.append(ENTITY_SUFFIXES.get(token, token))
            index += 1
    return out


def normalize_name(value: str | None) -> NormalizedName:
    """One organisation name in, a comparable name and a comparable stem out.

        >>> normalize_name("Acme Widgets, L.L.C.").norm
        'ACME WIDGETS LLC'
        >>> normalize_name("ACME WIDGETS LIMITED LIABILITY COMPANY").norm
        'ACME WIDGETS LLC'
        >>> normalize_name("Acme Widgets Inc.").stem
        'ACME WIDGETS'
        >>> normalize_name("North-Star & Sons, LLC").norm
        'NORTH STAR AND SONS LLC'
    """
    raw = value or ""
    if not raw.strip():
        return NormalizedName(raw=raw, norm="", stem="")

    tokens = _rejoin_dotted(_tokens(raw))
    tokens = _fold_suffixes(tokens)
    norm = " ".join(tokens)

    stem_tokens = list(tokens)
    while stem_tokens and stem_tokens[-1] in ENTITY_SUFFIXES:
        stem_tokens.pop()
    stem = " ".join(stem_tokens) or norm

    return NormalizedName(raw=raw, norm=norm, stem=stem)


def zip5(value: str | None) -> str | None:
    """The first five digits, or nothing.

    ZIP is the blocking key for every comparison: it has one job, to be equal
    for two records that could plausibly be the same place. ZIP+4 is therefore
    truncated - `12345` and `12345-6789` block together - and anything that is
    not five digits blocks with nothing rather than with everything.

        >>> zip5("12345-6789"), zip5("1234")
        ('12345', None)
    """
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits[:5] if len(digits) >= 5 else None
