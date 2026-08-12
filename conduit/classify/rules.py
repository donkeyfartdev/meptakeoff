"""Stage B rules: sheet number, discipline, subtype, confidence.

Pure functions over strings and numbers — no ORM session, no object store, no
PDF library. Everything here is a direct transcription of
``03-pipeline-specs.md`` §1.2, §1.3 and §1.7, and the three places where the
spec is silent or self-contradictory are called out below rather than being
quietly decided in code.

Why rules and not a model (§1.5): the sheet number is a near-deterministic
convention, we have zero training data, and a rule records *which rule fired* —
which is what ``ClassificationMethod`` and the ``sheet.classified`` audit
payload are for. A classifier here would be a black box in front of a regex.

Documented completions of the spec
----------------------------------
1. **Label prefixes.** §1.2 assumes the sheet number is its own text span. On
   real title blocks — and on the synthetic corpus — it arrives as
   ``"SHEET NUMBER:  M-102"``. So each span yields several *candidate* strings
   (the whole line; the line minus a recognised label; the last token) and the
   first candidate matching ``SHEET_NUMBER_RE`` wins. The regex itself is
   unchanged.
2. **Keyword matching is prefix-based for stems ≥ 4 characters.** §1.3 lists
   ``SCHEDULE`` and ``DUCT``, and real titles say ``SCHEDULES`` and
   ``DUCTWORK``. Short keys (``FA``, ``CW``, ``BAS``, ``AIR`` …) match whole
   words only, so ``FA`` does not swallow ``FAN`` and ``BAS`` does not swallow
   ``BASEMENT``.
3. **``title_agrees`` when the title votes for nothing.** §1.7 takes a bool and
   §1.6 only describes the disagreement case. A title with no discipline signal
   gives no independent corroboration, so it is treated as ``False`` (the
   -0.25 branch). That biases toward abstaining, which §1.8 says is the safe
   direction: a wrong sheet class propagates into every downstream quantity.

Also transcribed with a fix: §1.2's ``score_region`` computes ``small`` as
``mean(1.0 for ...) / len(inside)``, which is a typo — the mean of a bag of
1.0s is 1.0. It is implemented here as the fraction it obviously means: how
many spans in the region are small text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from conduit.db.models import ClassificationMethod, Discipline, SheetSubtype

__all__ = [
    "BASE_CONFIDENCE",
    "SHEET_NUMBER_RE",
    "SheetNumber",
    "classification_confidence",
    "discipline_for_prefix",
    "parse_sheet_number",
    "sheet_number_candidates",
    "subtype_for_title",
    "title_discipline_votes",
]

#: Verbatim from ``03-pipeline-specs.md`` §1.2.
SHEET_NUMBER_RE = re.compile(
    r"^(?P<disc>[A-Z]{1,3})[-. ]?(?P<num>\d{1,3})(?:\.(?P<sub>\d{1,2}))?(?P<suf>[A-Z])?$"
)

#: Labels that sit in front of the number in a title block.
_LABEL_RE = re.compile(
    r"^(?:SHEET|DWG|DRAWING|DRAWING\s+NUMBER)\s*(?:NO\.?|NUMBER|#)?\s*[:.\-]?\s*"
)

#: §1.3, prefix -> discipline. Longest prefix wins (``FP`` before ``F``).
DISCIPLINE_BY_PREFIX: dict[str, Discipline] = {
    "E": Discipline.ELECTRICAL,
    "EL": Discipline.ELECTRICAL,
    "EP": Discipline.ELECTRICAL,
    "ED": Discipline.ELECTRICAL,
    "ES": Discipline.ELECTRICAL,
    "FA": Discipline.ELECTRICAL,
    "P": Discipline.PLUMBING,
    "PL": Discipline.PLUMBING,
    "M": Discipline.MECHANICAL,
    "MH": Discipline.MECHANICAL,
    "H": Discipline.MECHANICAL,
    "FP": Discipline.FIRE_PROTECTION,
    "FS": Discipline.FIRE_PROTECTION,
    "A": Discipline.ARCHITECTURAL,
    "AD": Discipline.ARCHITECTURAL,
    "ID": Discipline.ARCHITECTURAL,
    "S": Discipline.STRUCTURAL,
    "C": Discipline.CIVIL,
    "CS": Discipline.CIVIL,
    "CU": Discipline.CIVIL,
    "G": Discipline.GENERAL,
    "T": Discipline.GENERAL,
    "GI": Discipline.GENERAL,
}

#: §1.3: a ``FA`` prefix forces the subtype regardless of the title vote.
FORCED_SUBTYPE_BY_PREFIX: dict[str, SheetSubtype] = {"FA": SheetSubtype.E_FIRE_ALARM}

#: §1.3 keyword vote, **in order — first match wins**. ``scope=None`` means the
#: rule applies to any discipline.
SUBTYPE_RULES: tuple[tuple[Discipline | None, tuple[str, ...], SheetSubtype], ...] = (
    (None, ("LEGEND", "SYMBOL", "ABBREVIATION"), SheetSubtype.LEGEND),
    (None, ("DETAIL", "SECTION", "ENLARGED DETAIL"), SheetSubtype.DETAIL),
    (None, ("COVER", "TITLE SHEET", "DRAWING INDEX"), SheetSubtype.COVER),
    (Discipline.ELECTRICAL, ("SCHEDULE",), SheetSubtype.E_SCHEDULE),
    (Discipline.PLUMBING, ("SCHEDULE",), SheetSubtype.P_SCHEDULE),
    (Discipline.MECHANICAL, ("SCHEDULE",), SheetSubtype.M_SCHEDULE),
    (Discipline.ELECTRICAL, ("ONE LINE", "SINGLE LINE", "RISER"), SheetSubtype.E_ONE_LINE),
    (Discipline.ELECTRICAL, ("LIGHTING", "LUMINAIRE", "RCP"), SheetSubtype.E_LIGHTING),
    (Discipline.ELECTRICAL, ("POWER", "RECEPTACLE", "DEVICE"), SheetSubtype.E_POWER),
    (Discipline.ELECTRICAL, ("FIRE ALARM", "FA"), SheetSubtype.E_FIRE_ALARM),
    (
        Discipline.ELECTRICAL,
        ("TELE", "DATA", "LOW VOLTAGE", "SECURITY"),
        SheetSubtype.E_LOW_VOLTAGE,
    ),
    (Discipline.ELECTRICAL, ("SITE", "CIVIL"), SheetSubtype.E_SITE),
    (Discipline.PLUMBING, ("RISER", "ISOMETRIC"), SheetSubtype.P_RISER),
    (Discipline.PLUMBING, ("SANITARY", "WASTE", "VENT", "DRAIN"), SheetSubtype.P_SANITARY),
    (Discipline.PLUMBING, ("DOMESTIC", "WATER", "CW", "HW"), SheetSubtype.P_DOMESTIC_WATER),
    (Discipline.PLUMBING, ("GAS", "MEDICAL GAS"), SheetSubtype.P_GAS),
    (Discipline.PLUMBING, ("STORM", "ROOF DRAIN"), SheetSubtype.P_STORM),
    (
        Discipline.MECHANICAL,
        ("DUCT", "AIR", "SUPPLY", "RETURN", "EXHAUST"),
        SheetSubtype.M_DUCT,
    ),
    (
        Discipline.MECHANICAL,
        ("PIPING", "HYDRONIC", "CHILLED", "HOT WATER"),
        SheetSubtype.M_PIPING,
    ),
    (Discipline.MECHANICAL, ("EQUIPMENT", "AHU", "RTU"), SheetSubtype.M_EQUIPMENT),
    (Discipline.MECHANICAL, ("CONTROL", "BAS", "ATC"), SheetSubtype.M_CONTROLS),
    (Discipline.FIRE_PROTECTION, ("SPRINKLER", "FIRE PROTECTION"), SheetSubtype.FP_SPRINKLER),
)

#: Discipline words that appear in titles, used only to decide whether the
#: title corroborates the sheet number (§1.6, §1.7). Not a classifier: the
#: prefix always wins the discipline itself.
TITLE_DISCIPLINE_WORDS: tuple[tuple[str, Discipline], ...] = (
    ("ELECTRICAL", Discipline.ELECTRICAL),
    ("LIGHTING", Discipline.ELECTRICAL),
    ("POWER", Discipline.ELECTRICAL),
    ("PANEL", Discipline.ELECTRICAL),
    ("FIRE ALARM", Discipline.ELECTRICAL),
    ("PLUMBING", Discipline.PLUMBING),
    ("SANITARY", Discipline.PLUMBING),
    ("DOMESTIC WATER", Discipline.PLUMBING),
    ("MECHANICAL", Discipline.MECHANICAL),
    ("HVAC", Discipline.MECHANICAL),
    ("DUCT", Discipline.MECHANICAL),
    ("FIRE PROTECTION", Discipline.FIRE_PROTECTION),
    ("SPRINKLER", Discipline.FIRE_PROTECTION),
    ("ARCHITECTURAL", Discipline.ARCHITECTURAL),
    ("STRUCTURAL", Discipline.STRUCTURAL),
    ("CIVIL", Discipline.CIVIL),
)

#: §1.7.
BASE_CONFIDENCE: dict[ClassificationMethod, float] = {
    ClassificationMethod.SHEET_NUMBER_REGEX: 0.85,
    ClassificationMethod.TITLE_BLOCK_KEYWORDS: 0.60,
    ClassificationMethod.THUMBNAIL_CLASSIFIER: 0.55,
    ClassificationMethod.DEFAULT_FALLBACK: 0.10,
    ClassificationMethod.HUMAN: 1.00,
}

_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Uppercase, punctuation stripped, whitespace collapsed (§1.3)."""
    return _WS.sub(" ", _PUNCT.sub(" ", title.upper())).strip()


def _keyword_matches(key: str, title_key: str) -> bool:
    """Whole-word for short stems, word-prefix for stems of 4+ characters."""
    if len(key.replace(" ", "")) <= 3:
        return re.search(rf"\b{re.escape(key)}\b", title_key) is not None
    return re.search(rf"\b{re.escape(key)}\w*", title_key) is not None


@dataclass(frozen=True, slots=True)
class SheetNumber:
    """A parsed sheet number and the pieces the rules key on."""

    raw: str
    prefix: str
    number: str
    sub: str | None = None
    suffix: str | None = None

    @property
    def normalised(self) -> str:
        """``E101`` -> ``E-101``; the form written to ``Sheet.sheet_number``."""
        out = f"{self.prefix}-{self.number}"
        if self.sub:
            out += f".{self.sub}"
        if self.suffix:
            out += self.suffix
        return out


def parse_sheet_number(text: str) -> SheetNumber | None:
    """Match one candidate string against ``SHEET_NUMBER_RE``."""
    m = SHEET_NUMBER_RE.match(text.strip())
    if m is None:
        return None
    return SheetNumber(
        raw=text.strip(),
        prefix=m.group("disc"),
        number=m.group("num"),
        sub=m.group("sub"),
        suffix=m.group("suf"),
    )


def sheet_number_candidates(normalized_text: str) -> list[str]:
    """Candidate substrings of one span that might be the sheet number.

    See completion 1 in the module docstring: the whole line, the line with a
    ``SHEET NUMBER:`` style label removed, and the trailing token.
    """
    text = normalized_text.strip()
    if not text:
        return []
    out = [text]
    stripped = _LABEL_RE.sub("", text).strip()
    if stripped and stripped != text:
        out.append(stripped)
    last = text.rsplit(" ", 1)[-1].strip()
    if last and last != text:
        out.append(last)
    return out


def discipline_for_prefix(prefix: str) -> Discipline:
    """§1.3. Unmapped prefixes are ``UNKNOWN`` — never a guess."""
    return DISCIPLINE_BY_PREFIX.get(prefix.upper(), Discipline.UNKNOWN)


def subtype_for_title(
    discipline: Discipline, title: str | None
) -> tuple[SheetSubtype, str | None]:
    """§1.3 keyword vote, scoped by the discipline already decided.

    Returns ``(subtype, matched_keyword)``; ``(OTHER, None)`` when nothing
    matched, which costs 0.10 of confidence in §1.7 rather than inventing a
    subtype.
    """
    if not title:
        return SheetSubtype.OTHER, None
    title_key = normalise_title(title)
    for scope, keys, subtype in SUBTYPE_RULES:
        if scope is not None and scope is not discipline:
            continue
        for key in keys:
            if _keyword_matches(key, title_key):
                return subtype, key
    return SheetSubtype.OTHER, None


def title_discipline_votes(title: str | None) -> set[Discipline]:
    """Which disciplines the title's own words point at (§1.6, §1.7).

    Used **only** for corroboration. The prefix decides the discipline: sheet
    numbers are checked by humans at issue time, titles are copy-pasted.
    """
    if not title:
        return set()
    title_key = normalise_title(title)
    return {d for word, d in TITLE_DISCIPLINE_WORDS if _keyword_matches(word, title_key)}


def classification_confidence(
    method: ClassificationMethod,
    *,
    title_agrees: bool,
    from_index: bool,
    subtype_matched: bool,
) -> float:
    """§1.7, transcribed. Ordinal, not probabilistic (§0)."""
    c = BASE_CONFIDENCE[method]
    if method is ClassificationMethod.HUMAN:
        return 1.0
    c += 0.10 if title_agrees else -0.25
    c *= 0.90 if from_index else 1.0
    c -= 0.0 if subtype_matched else 0.10
    return round(min(max(c, 0.0), 1.0), 4)
