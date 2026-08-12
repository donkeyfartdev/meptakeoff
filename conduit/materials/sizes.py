"""Canonical size representation for generated material names.

Two forms, and only two, exist for any size:

* ``Size.display`` — trade shorthand, the form an estimator reads and the form
  that appears inside a generated material name: ``1/2"``, ``1-1/4"``,
  ``3/4" x 1/2"`` (reducers), ``12x8`` (rectangular duct), ``#12 AWG``.
* ``Size.key`` — the join/aggregation form. It is **defined** as
  ``normalize_text(display)``, i.e. exactly what stage A already writes into
  ``TextSpan.normalized_text`` when it reads a size tag off a sheet. So
  ``1/2"`` keys as ``0.5IN`` and a size label read from the drawing joins to
  the vocabulary with an equality test, not a fuzzy match.

That invariant (``key == normalize_text(display)``) is not a convention we
intend to hold; it is asserted for every constructed ``Size`` in
``tests/test_materials_sizes.py``. If the stage A normaliser changes, those
tests fail, which is the intended alarm.

Known asymmetry, recorded rather than hidden: round/threaded sizes key in
``…IN`` form because the drawing writes an inch mark; rectangular duct keys as
``12X8`` because nobody writes inch marks on a duct tag and the normaliser only
converts marked inches. Both forms are stable and both round-trip; they are
simply different families.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from fractions import Fraction

from conduit.ingest.textlines import normalize_text

__all__ = [
    "TRADE_DENOMINATORS",
    "Size",
    "SizeKind",
    "format_inches",
    "parse_size",
]


class SizeKind(str, enum.Enum):
    """What a ``Size`` physically describes."""

    NONE = "none"                 # the item has no size (an aquastat, a lump sum)
    NOMINAL = "nominal"           # round/threaded nominal inches: 1/2", 2"
    REDUCER = "reducer"           # two nominal ends: 3/4" x 1/2"
    RECTANGULAR = "rectangular"   # width x height in inches: 12x8
    WIRE_GAUGE = "wire_gauge"     # #12 AWG, #4/0 AWG, 250 kcmil
    LABEL = "label"               # an opaque spec string: 75 gal, 1.28 GPF


#: Denominators a plan set actually uses for a nominal size. A value that does
#: not land on one of these is not a trade size and is rejected rather than
#: rendered as an odd fraction — an estimator reading ``1/10"`` learns nothing
#: except that we guessed.
TRADE_DENOMINATORS = frozenset({1, 2, 4, 8, 16, 32, 64})

_FRACTION_TOLERANCE = 1e-9

# 3/4  |  1-1/4  |  1 1/4  |  0.75  |  .75  |  2   — with an optional inch mark
# or IN suffix, which parse_size strips before this runs.
_NOMINAL_RE = re.compile(
    r"""^
    (?:(?P<whole>\d+)\s*[-\s]\s*)?      # optional whole part of a mixed number
    (?:
        (?P<num>\d+)\s*/\s*(?P<den>\d+) # a fraction
      | (?P<dec>\d*\.?\d+)              # or a decimal / integer
    )
    $""",
    re.VERBOSE,
)
_INCH_SUFFIX_RE = re.compile(r'\s*(?:"|”|IN|INCH(?:ES)?)\s*$', re.IGNORECASE)
_AWG_RE = re.compile(r"^#?\s*(?P<size>\d+(?:/0)?)\s*(?:AWG)?$", re.IGNORECASE)
_KCMIL_RE = re.compile(r"^(?P<size>\d+)\s*(?:KCMIL|MCM)$", re.IGNORECASE)
_RECT_RE = re.compile(r"^(?P<w>\d+(?:\.\d+)?)\s*[xX×]\s*(?P<h>\d+(?:\.\d+)?)$")


def format_inches(value: float) -> str:
    """``0.5 -> '1/2"'``, ``1.25 -> '1-1/4"'``, ``2.0 -> '2"'``.

    Raises ``ValueError`` for anything that is not a trade fraction.
    """
    if value <= 0:
        raise ValueError(f"size must be positive, got {value!r}")
    frac = Fraction(value).limit_denominator(max(TRADE_DENOMINATORS))
    if abs(float(frac) - value) > _FRACTION_TOLERANCE:
        raise ValueError(f"{value!r} is not representable as a trade fraction")
    if frac.denominator not in TRADE_DENOMINATORS:
        raise ValueError(
            f"{value!r} -> {frac} has denominator {frac.denominator}, "
            f"which is not a trade denominator {sorted(TRADE_DENOMINATORS)}"
        )
    whole, remainder = divmod(frac.numerator, frac.denominator)
    if remainder == 0:
        return f'{whole}"'
    if whole == 0:
        return f'{remainder}/{frac.denominator}"'
    return f'{whole}-{remainder}/{frac.denominator}"'


@dataclass(frozen=True, slots=True)
class Size:
    """A size in exactly one canonical display form and one canonical key."""

    display: str
    key: str
    kind: SizeKind
    primary_in: float | None = None
    secondary_in: float | None = None

    # -- constructors -------------------------------------------------------

    @staticmethod
    def _make(display: str, kind: SizeKind, primary: float | None = None,
              secondary: float | None = None) -> Size:
        return Size(
            display=display,
            key=normalize_text(display),
            kind=kind,
            primary_in=primary,
            secondary_in=secondary,
        )

    @classmethod
    def none(cls) -> Size:
        return cls(display="", key="", kind=SizeKind.NONE)

    @classmethod
    def nominal(cls, inches: float) -> Size:
        return cls._make(format_inches(inches), SizeKind.NOMINAL, primary=float(inches))

    @classmethod
    def reducer(cls, larger_in: float, smaller_in: float) -> Size:
        """``3/4" x 1/2"``. The larger end is always written first."""
        a, b = float(larger_in), float(smaller_in)
        if a < b:
            a, b = b, a
        display = f"{format_inches(a)} x {format_inches(b)}"
        return cls._make(display, SizeKind.REDUCER, primary=a, secondary=b)

    @classmethod
    def rectangular(cls, width_in: float, height_in: float) -> Size:
        """``12x8``. Written width-first, as the duct tag is."""
        return cls._make(
            f"{_plain(width_in)}x{_plain(height_in)}",
            SizeKind.RECTANGULAR,
            primary=float(width_in),
            secondary=float(height_in),
        )

    @classmethod
    def wire_gauge(cls, label: str) -> Size:
        """``#12 AWG``, ``#4/0 AWG``, ``250 kcmil`` — one spelling each."""
        text = label.strip().upper()
        kcmil = _KCMIL_RE.match(text)
        if kcmil:
            return cls._make(f"{int(kcmil.group('size'))} kcmil", SizeKind.WIRE_GAUGE)
        awg = _AWG_RE.match(text)
        if awg:
            return cls._make(f"#{awg.group('size')} AWG", SizeKind.WIRE_GAUGE)
        raise ValueError(f"not a recognised conductor size: {label!r}")

    @classmethod
    def label(cls, text: str) -> Size:
        """An opaque spec string (``75 gal``, ``1.28 GPF``). Never parsed."""
        cleaned = " ".join(text.split())
        return cls._make(cleaned, SizeKind.LABEL)

    # -- behaviour ----------------------------------------------------------

    def __bool__(self) -> bool:
        return self.kind is not SizeKind.NONE


def _plain(value: float) -> str:
    """``12.0 -> '12'``, ``7.5 -> '7.5'``."""
    return f"{value:g}"


def _parse_nominal_inches(text: str) -> float | None:
    stripped = _INCH_SUFFIX_RE.sub("", text.strip())
    match = _NOMINAL_RE.match(stripped)
    if not match:
        return None
    whole = float(match.group("whole") or 0)
    if match.group("num"):
        den = float(match.group("den"))
        if den == 0:
            return None
        return whole + float(match.group("num")) / den
    return whole + float(match.group("dec"))


def parse_size(text: str, *, prefer: SizeKind = SizeKind.NOMINAL) -> Size | None:
    """Best-effort text -> ``Size``. Returns ``None`` when nothing parses.

    ``prefer`` resolves the one genuine ambiguity in the notation: ``2 x 1``
    is a 2" x 1" reducer to a plumber and a 2"x1" duct to a sheet-metal
    estimator. The caller knows the ``SystemType`` of the run it is labelling,
    so it passes ``prefer=SizeKind.RECTANGULAR`` for duct. We never guess from
    the string alone; an unmarked pair defaults to ``prefer``.
    """
    if text is None:
        return None
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return None

    for pattern, ctor in ((_KCMIL_RE, Size.wire_gauge),):
        if pattern.match(cleaned):
            return ctor(cleaned)
    if cleaned.startswith("#") or cleaned.upper().endswith("AWG"):
        try:
            return Size.wire_gauge(cleaned)
        except ValueError:
            return None

    parts = re.split(r"\s*[xX×]\s*", cleaned)
    if len(parts) == 2:
        left, right = (_parse_nominal_inches(p) for p in parts)
        if left is None or right is None:
            return None
        marked = any(_INCH_SUFFIX_RE.search(p) or "/" in p for p in parts)
        if prefer is SizeKind.RECTANGULAR and not marked:
            return Size.rectangular(left, right)
        try:
            return Size.reducer(left, right)
        except ValueError:
            return None
    if len(parts) != 1:
        return None

    inches = _parse_nominal_inches(cleaned)
    if inches is None:
        return None
    try:
        return Size.nominal(inches)
    except ValueError:
        return None
