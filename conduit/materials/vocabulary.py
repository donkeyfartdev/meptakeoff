"""The controlled vocabulary. Material names are generated, never typed.

Why this module exists at all
----------------------------
The owner prices from a pricebook we do not own and will never own. Our output
therefore has to **join** to theirs. Free text does not join: ``1/2" cu 90``,
``1/2 copper ell`` and ``1/2" COPPER 90 ELL`` are three rows in a spreadsheet,
three lines in a bid, and one re-keying job for a human. So no material name in
this system is ever written by hand or by a model. Every name is *rendered*
from three registry entries:

    {size.display} {material.token} {item_type.token}
        1/2" copper 90        3/4" copper tee        2" PVC 45

and every name has a machine key that is what actually aggregates:

    item_key() -> "P|ELBOW_90|COPPER_WROT|0.5IN"

Two spellings of the same fitting therefore cannot produce two totals: they
either resolve to the same registry entries or they fail to resolve at all,
loudly. Resolution failure is a review item, not a new line.

Confirmation status is part of the data
---------------------------------------
Every entry records, per trade, where its wording came from:

* ``OWNER_SOURCED`` — the word appears in the owner's own takeoff workbook
  (``Commercial_Plumbing_Estimator_Takeoff_Sample.xlsx``, "Detailed Takeoff").
* ``TRADE_STANDARD`` — ordinary trade usage, not in that workbook, low risk.
* ``PROPOSED_PENDING_OWNER`` — **we made it up as a starting point.** Most of
  the electrical and HVAC wording is still in this state and lives in
  ``conduit/materials/proposed.py``. Do not treat any of it as agreed.
  ``pending_owner_confirmation()`` prints the list to be reviewed.

The owner has now confirmed four electrical words — ``LB``, ``condulet``,
``coupling``, ``connector`` — and one HVAC unit decision (duct is taken off by
the pound). Those live *here*, not in ``proposed.py``, because this file is the
confirmed vocabulary and that file is a proposal meant to be replaced wholesale.

Two structural rules the owner's answer forced
----------------------------------------------
**1. A family is not a line item.** An LB *is* a condulet — a conduit body. So
``condulet`` is modelled as the **family** (``ItemCategory.CONDUIT_BODY``) and
``LB`` as a **member item type** within it. Both words resolve, because
estimators say both, but only the member can become a line: ``resolve_family()``
returns a ``Family``, and a ``Family`` is not an ``ItemType``, so it cannot be
passed to ``item_key()`` (which refuses it at runtime, not merely in a type
hint) and cannot open an aggregation key. A bare "condulet" is therefore a
review item — an under-specified conduit body — and can never open a second
total beside the LB total for the same physical fitting.

Asking for a family word through the *item type* door is an explicit,
typed failure: ``resolve_item_type("condulet")`` raises
:class:`UnderSpecifiedTerm`, not ``None``. ``None`` from a resolver means "never
heard that word", and a family word is the opposite of unknown — returning
``None`` for it made a word an estimator actually says fail as
``AttributeError: 'NoneType' object has no attribute 'code'`` at whatever
distant line first touched the result. ``UnderSpecifiedTerm`` subclasses
``AmbiguousTerm``, so both senses of "did not resolve to exactly one thing" are
caught by one ``except``, and it carries ``.family`` and ``.candidates`` so the
review UI can show the five conduit bodies rather than a stack trace.

**2. ``LB`` is ambiguous and is never guessed.** ``LB`` is the electrical
conduit body *and* ``UnitOfMeasure.POUNDS`` is spelled ``LB`` — and duct is now
taken off by the pound, so both meanings are live in the same export. They are
kept structurally apart: one is an ``ItemType`` whose ``code`` is
``CONDULET_LB``, the other is an enum member in a different column, so no
aggregation key can ever mix them. On top of that, any *string* lookup that
could receive either raises :class:`AmbiguousTerm` unless the caller supplies a
``discipline`` or an explicit ``prefer=``, exactly as ``sizes.py`` does for the
``2 x 1`` reducer/duct ambiguity.

There is no accuracy claim anywhere in this module. Whether these are the words
estimators use is a question for estimators; the code's only job is to make the
words swappable in one place and the totals stable once they are agreed.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from conduit.db.models import Discipline, UnitOfMeasure
from conduit.ingest.textlines import normalize_text
from conduit.materials.sizes import Size, SizeKind, parse_size

__all__ = [
    "AMBIGUOUS_TERMS",
    "FAMILIES",
    "VOCABULARY_VERSION",
    "ITEM_TYPES",
    "MATERIALS",
    "AmbiguousTerm",
    "Family",
    "ItemCategory",
    "ItemType",
    "Material",
    "ParsedItemName",
    "Registry",
    "TermKind",
    "UnderSpecifiedTerm",
    "WEIGHT_CATEGORIES",
    "VocabStatus",
    "alias_key",
    "default_uom",
    "family_members",
    "item_key",
    "parse_item_name",
    "pending_owner_confirmation",
    "render_item_name",
    "resolve_family",
    "resolve_item_type",
    "resolve_material",
    "resolve_term",
    "resolve_unit",
]

#: Bump whenever a token, an alias or a key format changes. Written to
#: ``PipelineRun.model_versions["vocabulary"]`` so an old export stays
#: explicable after the words move.
#: ``vocab-2``: the owner's confirmed electrical words (LB, condulet, coupling,
#: connector), the ``CONDUIT_BODY`` family, and duct priced by the pound.
VOCABULARY_VERSION = "vocab-2"


class VocabStatus(str, enum.Enum):
    OWNER_SOURCED = "owner_sourced"
    TRADE_STANDARD = "trade_standard"
    PROPOSED_PENDING_OWNER = "proposed_pending_owner_confirmation"


class ItemCategory(str, enum.Enum):
    """What kind of thing a line item is. Decides the default unit."""

    PIPE = "pipe"
    FITTING = "fitting"
    VALVE = "valve"
    FIXTURE = "fixture"
    EQUIPMENT = "equipment"
    CONDUIT = "conduit"
    CONDUIT_BODY = "conduit_body"
    WIRE = "wire"
    #: Fabricated sheet-metal duct. Owner-confirmed as a **weight** category:
    #: "duct gets taken off by the pound". See ``_CATEGORY_UOM``.
    DUCT = "duct"
    DEVICE = "device"
    #: Flexible duct, which is bought by the foot off a roll rather than
    #: fabricated from sheet, so it is *not* in the weight category. Separating
    #: it is what lets the unit stay a property of the category (§1.5 of
    #: ``docs/output-schema.md``) instead of becoming a per-line choice.
    DUCT_FLEX = "duct_flex"
    DUCT_FITTING = "duct_fitting"
    HANGER = "hanger"
    INSULATION = "insulation"
    ACCESSORY = "accessory"
    ALLOWANCE = "allowance"


#: Category -> unit. **The unit is a property of the category and is never
#: chosen per line** — that is what stops a fitting arriving as ``LF`` because
#: a writer mistyped a row, and it is why the owner's "duct by the pound"
#: answer is one edit here rather than a rule at every call site.
#:
#: Three kinds of category, and now three units:
#:
#: * linear (pipe, conduit, wire, flex duct, insulation) -> ``LF``
#: * discrete (fittings, conduit bodies, valves, fixtures, devices,
#:   equipment, hangers, duct fittings) -> ``EA``
#: * **weight** (fabricated sheet-metal duct) -> ``LB``, owner-directed:
#:   "duct gets taken off by the pound". How the pounds are arrived at is
#:   specified in ``docs/derived-quantities.md`` §6.6 — it is a computation
#:   over geometry and gauge, and the gauge is the weak link.
#:
#: NOTE: the owner's workbook writes ``LS`` for lump sum and ``UnitOfMeasure``
#: has no ``LS`` member — it has ``LOT``. See ``docs/output-schema.md`` §6; the
#: enum member is still listed as missing and is not added here.
_CATEGORY_UOM: Mapping[ItemCategory, UnitOfMeasure] = {
    ItemCategory.PIPE: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.CONDUIT: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.WIRE: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.DUCT_FLEX: UnitOfMeasure.LINEAR_FEET,
    # Pipe insulation is LF. Duct liner and duct wrap are also in this
    # category and the trade commonly takes them off by the square foot —
    # unconfirmed, listed as an open question (``docs/output-schema.md`` §8).
    ItemCategory.INSULATION: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.DUCT: UnitOfMeasure.POUNDS,
    ItemCategory.FITTING: UnitOfMeasure.EACH,
    ItemCategory.CONDUIT_BODY: UnitOfMeasure.EACH,
    ItemCategory.DUCT_FITTING: UnitOfMeasure.EACH,
    ItemCategory.VALVE: UnitOfMeasure.EACH,
    ItemCategory.FIXTURE: UnitOfMeasure.EACH,
    ItemCategory.EQUIPMENT: UnitOfMeasure.EACH,
    ItemCategory.DEVICE: UnitOfMeasure.EACH,
    ItemCategory.HANGER: UnitOfMeasure.EACH,
    ItemCategory.ACCESSORY: UnitOfMeasure.EACH,
    ItemCategory.ALLOWANCE: UnitOfMeasure.LOT,
}

#: The categories whose quantity is a weight rather than a count or a length.
#: Named rather than inferred, because "is this line pounds?" is asked in two
#: places (the export writer and the duct-weight rule) and must not be two
#: different answers.
WEIGHT_CATEGORIES: frozenset[ItemCategory] = frozenset({ItemCategory.DUCT})


def default_uom(category: ItemCategory) -> UnitOfMeasure:
    return _CATEGORY_UOM[category]


_ALIAS_STRIP = re.compile(r"[^A-Z0-9/#]+")


def alias_key(text: str) -> str:
    """Fold a written spelling to its lookup key.

    Uppercase and whitespace-collapse via the *same* normaliser stage A uses,
    then drop punctuation that carries no meaning here, so ``90°``, ``90 deg``
    and ``90`` all land on ``90``, and ``ELL``/``ell`` on ``ELL``. Inch marks
    survive normalisation as ``IN`` and are kept, because ``1/2"`` and ``1/2``
    genuinely are the same size and must fold together.
    """
    return _ALIAS_STRIP.sub("", normalize_text(text))


@dataclass(frozen=True, slots=True)
class Material:
    """A material as it is *written* in a name, plus where the wording is from.

    ``token`` is the exact string that appears in a rendered name. Casing is
    deliberate and preserved: ``copper`` is lower case, ``PVC`` and ``EMT`` are
    acronyms.
    """

    code: str
    token: str
    trades: tuple[tuple[Discipline, VocabStatus], ...]
    aliases: tuple[str, ...] = ()
    note: str | None = None

    def status_for(self, trade: Discipline) -> VocabStatus | None:
        for discipline, status in self.trades:
            if discipline is trade:
                return status
        return None

    @property
    def disciplines(self) -> tuple[Discipline, ...]:
        return tuple(d for d, _ in self.trades)


@dataclass(frozen=True, slots=True)
class ItemType:
    """A fitting/valve/fixture/equipment word, per trade."""

    code: str
    token: str
    category: ItemCategory
    trades: tuple[tuple[Discipline, VocabStatus], ...]
    aliases: tuple[str, ...] = ()
    #: The size family this item takes, when it is constrained. ``None`` means
    #: any (or none). A reducer is the only item that *requires* two ends.
    size_kind: SizeKind | None = None
    note: str | None = None

    def status_for(self, trade: Discipline) -> VocabStatus | None:
        for discipline, status in self.trades:
            if discipline is trade:
                return status
        return None

    @property
    def disciplines(self) -> tuple[Discipline, ...]:
        return tuple(d for d, _ in self.trades)

    @property
    def uom(self) -> UnitOfMeasure:
        return default_uom(self.category)


@dataclass(frozen=True, slots=True)
class Family:
    """A word for a *class* of fitting, which is deliberately not a line item.

    Estimators say both "LB" and "condulet", and an LB **is** a condulet — a
    conduit body. The two are not siblings: one is the family, the other a
    member of it. Modelling them as siblings would be the same defect PR #4
    fixed in the aggregation key, one column over — two rows that are one
    physical fitting, summing separately, with nothing in the export saying so.

    So a ``Family`` resolves (the word is known, and saying "unknown word" to an
    estimator who wrote a correct trade term is its own kind of wrong) but it is
    **not** an ``ItemType``: ``item_key()`` will not take it, so it can never
    produce a total. A quantity that arrives as a bare family word is
    under-specified, and under-specified is a review item — the same handling as
    an unresolvable name, for the same reason.

    Which member an unqualified "condulet" *means* is not inferable: an LB, an
    LL, an LR, a T and a C are all condulets, they cost differently, and the
    drawing that says "condulet" has not said which. Defaulting to LB because it
    is the common one would be a guess wearing a count's clothing.
    """

    code: str
    token: str
    category: ItemCategory
    trades: tuple[tuple[Discipline, VocabStatus], ...]
    aliases: tuple[str, ...] = ()
    note: str | None = None

    def status_for(self, trade: Discipline) -> VocabStatus | None:
        for discipline, status in self.trades:
            if discipline is trade:
                return status
        return None

    @property
    def disciplines(self) -> tuple[Discipline, ...]:
        return tuple(d for d, _ in self.trades)


class TermKind(str, enum.Enum):
    """What kind of thing a written term resolved to — or should resolve to.

    Used as the ``prefer=`` argument for the one genuinely ambiguous term in
    the vocabulary, mirroring ``sizes.parse_size(prefer=SizeKind…)``.
    """

    ITEM_TYPE = "item_type"
    MATERIAL = "material"
    FAMILY = "family"
    UNIT = "unit"


class AmbiguousTerm(ValueError):
    """A written term has two meanings here and the caller did not say which.

    Raised rather than resolved. The alternative — pick the likelier one — is
    how ``LB`` (a conduit body) and ``LB`` (a pound of duct) end up in the same
    total, which is precisely the failure this vocabulary exists to prevent.
    """


class UnderSpecifiedTerm(AmbiguousTerm):
    """A real trade word that names a *family*, asked for as a single item type.

    ``resolve_item_type("condulet")`` must not return ``None``. ``None`` is this
    module's answer for "I have never heard that word", and a caller then does
    ``.code`` and gets ``AttributeError: 'NoneType' object has no attribute
    'code'`` somewhere far away from the string that caused it. "Condulet" is a
    word estimators say and the owner named; the vocabulary knows it perfectly
    well. What it does not know is *which* condulet — and unlike ``"LB"``, no
    argument the caller can pass will settle that, because the missing
    information is missing from the drawing, not from the call.

    So the failure is raised here, typed, and carries the family plus its
    candidate members so the review UI can put that list in front of a human
    instead of a stack trace.

    It subclasses :class:`AmbiguousTerm` deliberately: a caller that already
    handles "this string did not resolve to exactly one thing" needs no change,
    while a caller that can offer the choice catches this specifically. The two
    are genuinely different failures — ``AmbiguousTerm("LB")`` is fixable by the
    caller with ``discipline=``, ``UnderSpecifiedTerm("condulet")`` is fixable
    only by asking someone which fitting the drawing meant.
    """

    def __init__(
        self,
        term: str,
        family: Family,
        candidates: tuple[ItemType, ...] = (),
    ) -> None:
        self.term = term
        self.family = family
        self.candidates = candidates
        listed = [c.code for c in candidates] or ["<no members registered>"]
        super().__init__(
            f"{term!r} names the {family.code} family, not one item type: it "
            f"means one of {listed} and the drawing has not said which. A family "
            f"cannot open an aggregation key (see Family), so this is a review "
            f"item — offer family_members() to a human, or call resolve_family() "
            f"if the family itself is what you wanted."
        )


#: Terms with more than one meaning across the whole vocabulary, and the kinds
#: they span. Exactly one entry today, and it is load-bearing:
#:
#:   ``LB``  = the electrical conduit body (``ItemType`` ``CONDULET_LB``)
#:   ``LB``  = the pound (``UnitOfMeasure.POUNDS``), the unit fabricated duct
#:             is now taken off in
#:
#: The two are already *structurally* separate — one is an item type whose code
#: is ``CONDULET_LB`` and appears in an aggregation key, the other is an enum
#: member that appears in the ``Unit`` column — so no arithmetic can mix them.
#: This table guards the remaining hole: a **string** arriving from a schedule
#: cell, a legend, or a re-imported export, where nothing but the discipline
#: says which was meant.
AMBIGUOUS_TERMS: Mapping[str, tuple[TermKind, ...]] = {
    "LB": (TermKind.ITEM_TYPE, TermKind.UNIT),
}

#: How a discipline settles an ambiguous term. This is a stated rule, not an
#: inference: conduit bodies exist only in electrical, and only mechanical
#: takes material off by weight. A discipline that is in neither position
#: (plumbing) does **not** disambiguate and gets the exception.
_AMBIGUOUS_BY_DISCIPLINE: Mapping[str, Mapping[Discipline, TermKind]] = {
    "LB": {
        Discipline.ELECTRICAL: TermKind.ITEM_TYPE,
        Discipline.MECHANICAL: TermKind.UNIT,
    },
}


_E = Discipline.ELECTRICAL
_P = Discipline.PLUMBING
_M = Discipline.MECHANICAL
_OWNER = VocabStatus.OWNER_SOURCED
_STD = VocabStatus.TRADE_STANDARD
_PROPOSED = VocabStatus.PROPOSED_PENDING_OWNER


class Registry:
    """Code- and alias-indexed lookup. Duplicate aliases are a hard error.

    A silently shadowed alias is exactly the failure this module exists to
    prevent, so the collision is raised at import time rather than discovered
    when two totals disagree.
    """

    def __init__(self, name: str, entries: Iterable[Material | ItemType]) -> None:
        self.name = name
        self._by_code: dict[str, Material | ItemType] = {}
        self._by_alias: dict[str, Material | ItemType] = {}
        for entry in entries:
            self.add(entry)

    def add(self, entry: Material | ItemType) -> None:
        if entry.code in self._by_code:
            raise ValueError(f"{self.name}: duplicate code {entry.code!r}")
        self._by_code[entry.code] = entry
        for spelling in (entry.code, entry.token, *entry.aliases):
            key = alias_key(spelling)
            if not key:
                continue
            existing = self._by_alias.get(key)
            if existing is not None and existing.code != entry.code:
                raise ValueError(
                    f"{self.name}: alias {spelling!r} -> {key!r} claimed by both "
                    f"{existing.code} and {entry.code}"
                )
            self._by_alias[key] = entry

    def __contains__(self, code: object) -> bool:
        return code in self._by_code

    def __iter__(self) -> Iterator[Material | ItemType]:
        return iter(self._by_code.values())

    def __len__(self) -> int:
        return len(self._by_code)

    def get(self, code: str):
        return self._by_code.get(code)

    def lookup(self, spelling: str):
        """Resolve any accepted spelling. ``None`` when the word is unknown."""
        return self._by_alias.get(alias_key(spelling))

    def codes(self) -> tuple[str, ...]:
        return tuple(self._by_code)

    def for_trade(self, trade: Discipline) -> tuple[Material | ItemType, ...]:
        return tuple(e for e in self._by_code.values() if trade in e.disciplines)


# ---------------------------------------------------------------------------
# Plumbing — seeded from the owner's workbook and standard trade usage
# ---------------------------------------------------------------------------

PLUMBING_MATERIALS: tuple[Material, ...] = (
    Material("COPPER_TYPE_L", "Type L copper", ((_P, _OWNER),),
             aliases=("type l cu", "l copper", "copper type l")),
    Material("COPPER_TYPE_M", "Type M copper", ((_P, _STD),), aliases=("type m cu",)),
    Material("COPPER_TYPE_K", "Type K copper", ((_P, _STD),), aliases=("type k cu",)),
    Material("COPPER_WROT", "copper", ((_P, _OWNER),),
             aliases=("cu", "wrot copper", "wrought copper"),
             note="The fitting material. Type L/M/K describes tube, not fittings."),
    Material("PVC_SCH40", "PVC", ((_P, _OWNER), (_E, _PROPOSED), (_M, _PROPOSED)),
             aliases=("pvc sch 40", "schedule 40 pvc"),
             note="Also the electrical PVC conduit material and HVAC condensate "
                  "piping. Shared rather than duplicated: one word, one token, "
                  "one key. Its E/M status is PROPOSED like everything else "
                  "in those trades."),
    Material("PVC_DWV", "PVC DWV", ((_P, _OWNER),), aliases=("dwv pvc",)),
    Material("CPVC", "CPVC", ((_P, _STD),)),
    Material("ABS", "ABS", ((_P, _STD),)),
    Material("PEX", "PEX", ((_P, _STD),)),
    Material("CAST_IRON_NO_HUB", "no-hub CI", ((_P, _OWNER),),
             aliases=("no hub cast iron", "nh ci", "no-hub cast iron")),
    Material("CAST_IRON", "CI", ((_P, _OWNER),), aliases=("cast iron",)),
    Material("BRASS", "brass", ((_P, _OWNER),)),
    Material("BRASS_LEAD_FREE", "lead-free brass", ((_P, _OWNER),),
             aliases=("brass lf", "lf brass", "lead free brass")),
    Material("BRONZE", "bronze", ((_P, _OWNER),)),
    Material("STAINLESS", "SS", ((_P, _OWNER), (_M, _PROPOSED)), aliases=("stainless steel",)),
    Material("GALV_STEEL", "galv steel", ((_P, _STD), (_M, _PROPOSED)),
             aliases=("galvanized steel", "galvanised steel")),
    Material("BLACK_STEEL", "black steel", ((_P, _STD),), aliases=("black iron",)),
    Material("FIBERGLASS", "fiberglass", ((_P, _OWNER), (_M, _PROPOSED)),
             aliases=("fibreglass",)),
    Material("VITREOUS_CHINA", "vitreous china", ((_P, _OWNER),)),
    Material("PORCELAIN", "porcelain", ((_P, _OWNER),),
             aliases=("commercial porcelain",)),
)

PLUMBING_ITEM_TYPES: tuple[ItemType, ...] = (
    # --- linear ---
    ItemType("PIPE", "pipe", ItemCategory.PIPE, ((_P, _OWNER),)),
    ItemType("TUBE", "tube", ItemCategory.PIPE, ((_P, _OWNER),)),
    ItemType("INSULATION", "insulation", ItemCategory.INSULATION,
             ((_P, _OWNER), (_M, _PROPOSED))),
    # --- fittings ---
    ItemType("ELBOW_90", "90", ItemCategory.FITTING, ((_P, _OWNER), (_E, _PROPOSED)),
             aliases=("ell", "el", "elbow", "90 ell", "90 deg", "90 degree",
                      "quarter bend", "1/4 bend", "90 elbow")),
    ItemType("ELBOW_45", "45", ItemCategory.FITTING, ((_P, _OWNER), (_E, _PROPOSED)),
             aliases=("45 ell", "45 deg", "eighth bend", "1/8 bend", "45 elbow")),
    ItemType("ELBOW_22", "22-1/2", ItemCategory.FITTING, ((_P, _STD),),
             aliases=("22 1/2", "sixteenth bend", "1/16 bend")),
    ItemType("ELBOW_STREET_90", "street 90", ItemCategory.FITTING, ((_P, _STD),),
             aliases=("street ell", "st 90")),
    ItemType("LONG_SWEEP_90", "long sweep 90", ItemCategory.FITTING, ((_P, _STD),),
             aliases=("long turn 90", "sweep 90")),
    ItemType("TEE", "tee", ItemCategory.FITTING,
             ((_P, _OWNER), (_E, _PROPOSED), (_M, _PROPOSED)), aliases=("t",)),
    ItemType("TEE_REDUCING", "reducing tee", ItemCategory.FITTING, ((_P, _STD),),
             aliases=("red tee", "bull head tee")),
    ItemType("SANITARY_TEE", "san tee", ItemCategory.FITTING, ((_P, _STD),),
             aliases=("sanitary tee",)),
    ItemType("WYE", "wye", ItemCategory.FITTING, ((_P, _OWNER),), aliases=("y",)),
    ItemType("COMBO_WYE_EIGHTH", "combo wye and 1/8 bend", ItemCategory.FITTING,
             ((_P, _STD),), aliases=("combo", "combination wye 1/8 bend")),
    ItemType("CROSS", "cross", ItemCategory.FITTING, ((_P, _STD),)),
    # Electrical status is OWNER_SOURCED: the owner named "coupling" as one of
    # the fitting words their estimators use.
    #
    # ASSUMPTION, recorded so it is visible if wrong: because the owner listed
    # coupling as a *fitting word*, conduit couplings are **counted as their own
    # line item** and are not folded into a per-100-ft raceway allowance. That
    # is the question `docs/output-schema.md` §8 asked, and this is the reading
    # of the answer. It settles that couplings get a line; it does **not**
    # settle how the quantity on that line is arrived at — for conduit and for
    # hard-drawn copper the count still comes from stock length, which is
    # `factored` and says so (`docs/derived-quantities.md` §6.2). If the owner
    # meant "counted off the drawings", §6.2 is what changes, not this entry.
    ItemType("COUPLING", "coupling", ItemCategory.FITTING,
             ((_P, _OWNER), (_E, _OWNER)), aliases=("cplg", "coup")),
    ItemType("UNION", "union", ItemCategory.FITTING, ((_P, _OWNER),)),
    ItemType("DIELECTRIC_UNION", "dielectric union", ItemCategory.FITTING, ((_P, _OWNER),),
             aliases=("di union", "dielectric")),
    ItemType("REDUCER", "reducer", ItemCategory.FITTING,
             ((_P, _OWNER), (_E, _PROPOSED), (_M, _PROPOSED)),
             aliases=("red", "reducing coupling"), size_kind=SizeKind.REDUCER),
    ItemType("BUSHING", "bushing", ItemCategory.FITTING,
             ((_P, _STD), (_E, _PROPOSED)), aliases=("bush",)),
    ItemType("CAP", "cap", ItemCategory.FITTING, ((_P, _OWNER), (_E, _PROPOSED))),
    ItemType("PLUG", "plug", ItemCategory.FITTING, ((_P, _STD),)),
    ItemType("ADAPTER", "adapter", ItemCategory.FITTING, ((_P, _STD),),
             aliases=("adaptor", "male adapter", "female adapter")),
    ItemType("NIPPLE", "nipple", ItemCategory.FITTING, ((_P, _STD),)),
    ItemType("P_TRAP", "P-trap", ItemCategory.FITTING, ((_P, _STD),),
             aliases=("p trap", "trap")),
    ItemType("CLOSET_FLANGE", "closet flange", ItemCategory.FITTING, ((_P, _STD),)),
    ItemType("NO_HUB_COUPLING", "no-hub coupling", ItemCategory.FITTING, ((_P, _OWNER),),
             aliases=("no hub coupling", "band clamp", "shielded coupling")),
    # --- valves and specialties ---
    ItemType("BALL_VALVE", "ball valve", ItemCategory.VALVE, ((_P, _OWNER),),
             aliases=("bv", "full port ball valve")),
    ItemType("GATE_VALVE", "gate valve", ItemCategory.VALVE, ((_P, _STD),)),
    ItemType("GLOBE_VALVE", "globe valve", ItemCategory.VALVE, ((_P, _STD),)),
    ItemType("BUTTERFLY_VALVE", "butterfly valve", ItemCategory.VALVE, ((_P, _STD),)),
    ItemType("CHECK_VALVE_SWING", "swing check valve", ItemCategory.VALVE, ((_P, _OWNER),),
             aliases=("swing check", "check valve swing")),
    ItemType("CHECK_VALVE_SPRING", "spring check valve", ItemCategory.VALVE, ((_P, _STD),),
             aliases=("spring check",)),
    ItemType("BALANCING_VALVE", "balancing valve", ItemCategory.VALVE, ((_P, _STD),)),
    ItemType("STOP_ANGLE", "angle stop", ItemCategory.VALVE, ((_P, _STD),),
             aliases=("angle supply stop",)),
    ItemType("PRV", "PRV", ItemCategory.VALVE, ((_P, _OWNER),),
             aliases=("pressure reducing valve", "prv assembly")),
    ItemType("BACKFLOW_RPZ", "RPZ backflow preventer", ItemCategory.VALVE, ((_P, _OWNER),),
             aliases=("rpz", "reduced pressure zone backflow preventer", "rp backflow")),
    ItemType("MIXING_VALVE_THERMOSTATIC", "thermostatic mixing valve", ItemCategory.VALVE,
             ((_P, _OWNER),), aliases=("tmv", "mixing valve")),
    ItemType("VACUUM_BREAKER", "vacuum breaker", ItemCategory.VALVE, ((_P, _OWNER),),
             aliases=("avb", "air vent vacuum breaker")),
    ItemType("HOSE_BIBB", "hose bibb", ItemCategory.VALVE, ((_P, _OWNER),),
             aliases=("hose bib", "hosebibb")),
    ItemType("WALL_HYDRANT", "wall hydrant", ItemCategory.VALVE, ((_P, _OWNER),),
             aliases=("freeze proof wall hydrant",)),
    ItemType("WATER_HAMMER_ARRESTOR", "water hammer arrestor", ItemCategory.ACCESSORY,
             ((_P, _OWNER),), aliases=("wha", "hammer arrestor", "shock absorber")),
    ItemType("GAUGE_COCK", "gauge cock", ItemCategory.ACCESSORY, ((_P, _OWNER),),
             aliases=("gage cock",)),
    ItemType("PRESSURE_GAUGE", "pressure gauge", ItemCategory.ACCESSORY, ((_P, _OWNER),),
             aliases=("pressure gage",)),
    # --- fixtures ---
    ItemType("WATER_CLOSET", "water closet", ItemCategory.FIXTURE, ((_P, _OWNER),),
             aliases=("wc", "toilet")),
    ItemType("URINAL", "urinal", ItemCategory.FIXTURE, ((_P, _OWNER),), aliases=("ur",)),
    ItemType("LAVATORY", "lavatory", ItemCategory.FIXTURE, ((_P, _OWNER),),
             aliases=("lav",)),
    ItemType("SINK", "sink", ItemCategory.FIXTURE, ((_P, _OWNER),)),
    ItemType("MOP_SINK", "mop sink", ItemCategory.FIXTURE, ((_P, _OWNER),),
             aliases=("mop basin", "service sink")),
    ItemType("FLOOR_DRAIN", "floor drain", ItemCategory.FIXTURE, ((_P, _OWNER),),
             aliases=("fd",)),
    ItemType("FLOOR_SINK", "floor sink", ItemCategory.FIXTURE, ((_P, _OWNER),),
             aliases=("fs",)),
    ItemType("DRINKING_FOUNTAIN", "drinking fountain", ItemCategory.FIXTURE, ((_P, _OWNER),),
             aliases=("df", "ewc", "bottle filler")),
    ItemType("SHOWER", "shower", ItemCategory.FIXTURE, ((_P, _STD),)),
    ItemType("EYEWASH", "eyewash", ItemCategory.FIXTURE, ((_P, _STD),),
             aliases=("emergency eyewash",)),
    ItemType("CLEANOUT", "cleanout", ItemCategory.FIXTURE, ((_P, _OWNER),),
             aliases=("co", "floor cleanout")),
    ItemType("CARRIER", "carrier", ItemCategory.ACCESSORY, ((_P, _OWNER),),
             aliases=("fixture carrier", "chair carrier")),
    ItemType("TRAP_PRIMER", "trap primer", ItemCategory.ACCESSORY, ((_P, _OWNER),)),
    ItemType("ROOF_VENT_TERMINAL", "roof vent terminal", ItemCategory.ACCESSORY,
             ((_P, _OWNER),), aliases=("vtr", "vent thru roof", "roof flashing")),
    # --- equipment ---
    ItemType("WATER_HEATER", "water heater", ItemCategory.EQUIPMENT, ((_P, _OWNER),),
             aliases=("wh",)),
    ItemType("RECIRC_PUMP", "recirculation pump", ItemCategory.EQUIPMENT, ((_P, _OWNER),),
             aliases=("recirc pump",)),
    ItemType("EXPANSION_TANK", "expansion tank", ItemCategory.EQUIPMENT, ((_P, _OWNER),),
             aliases=("exp tank",)),
    # --- supports ---
    ItemType("HANGER", "hanger", ItemCategory.HANGER,
             ((_P, _OWNER), (_E, _PROPOSED), (_M, _PROPOSED)),
             aliases=("pipe hanger", "support", "clevis hanger")),
    ItemType("SLEEVE", "sleeve", ItemCategory.ACCESSORY, ((_P, _OWNER),)),
    ItemType("FIRESTOP", "firestopping", ItemCategory.ACCESSORY, ((_P, _OWNER),),
             aliases=("firestop", "fire stopping")),
    ItemType("ALLOWANCE", "allowance", ItemCategory.ALLOWANCE,
             ((_P, _STD), (_E, _PROPOSED), (_M, _PROPOSED)),
             aliases=("lump sum", "ls item")),
)


# ---------------------------------------------------------------------------
# Electrical — the words the owner confirmed
# ---------------------------------------------------------------------------
#
# Owner, verbatim in substance: *"the electrical fitting words my estimators use
# — LB, condulet, coupling, connector."*
#
# Four words. They live here rather than in ``proposed.py`` because that file is
# a proposal meant to be thrown away wholesale, and these are decisions.
# ``coupling`` is in ``PLUMBING_ITEM_TYPES`` above — one word, one entry, one
# key, now owner-sourced for both trades. Everything else electrical stays in
# ``proposed.py`` and stays ``PROPOSED_PENDING_OWNER``.

ELECTRICAL_ITEM_TYPES: tuple[ItemType, ...] = (
    ItemType("CONNECTOR", "connector", ItemCategory.FITTING, ((_E, _OWNER),),
             aliases=("conduit connector", "set screw connector",
                      "compression connector"),
             note="Owner-confirmed word. Whether a connector is counted per "
                  "raceway termination or only where drawn is a measurement "
                  "question, not a vocabulary one — see derived-quantities."),
    # The one member of the CONDUIT_BODY family the owner named. Its ``code``
    # is CONDULET_LB, never the bare token "LB": the code is what enters an
    # aggregation key, and a key component spelled "LB" would sit one column
    # away from a Unit column spelled "LB". Different strings by construction.
    ItemType("CONDULET_LB", "LB", ItemCategory.CONDUIT_BODY, ((_E, _OWNER),),
             aliases=("lb condulet", "condulet lb", "type lb", "lb body",
                      "lb conduit body"),
             note="A back-outlet conduit body. Member of the CONDUIT_BODY "
                  "family whose family word is 'condulet' — see Family."),
)


# ---------------------------------------------------------------------------
# Families — words for a class of fitting, which are not line items
# ---------------------------------------------------------------------------

CONDUIT_BODY_FAMILY = Family(
    code="CONDUIT_BODY",
    token="condulet",
    category=ItemCategory.CONDUIT_BODY,
    trades=((_E, _OWNER),),
    aliases=("condulets", "conduit body", "conduit bodies", "condulet body"),
    note=(
        "The owner named both 'LB' and 'condulet'. An LB *is* a condulet, so "
        "they are family and member rather than siblings: 'condulet' resolves "
        "here and cannot become a line, 'LB' resolves to the CONDULET_LB item "
        "type and can. That is what stops one physical fitting producing two "
        "totals. 'Condulet' is a Crouse-Hinds trade name in general trade use; "
        "'conduit body' is the generic term and is an alias of the same family."
    ),
)

#: All families. One today.
FAMILIES: tuple[Family, ...] = (CONDUIT_BODY_FAMILY,)

_FAMILY_BY_ALIAS: dict[str, Family] = {}
for _family in FAMILIES:
    for _spelling in (_family.code, _family.token, *_family.aliases):
        _key = alias_key(_spelling)
        if _key:
            _FAMILY_BY_ALIAS[_key] = _family
del _family, _spelling, _key


def _build_registries() -> tuple[Registry, Registry]:
    # Imported here rather than at module top so the proposed vocabulary is
    # visibly a separate, swappable input rather than part of this file.
    from conduit.materials import proposed

    materials = Registry("materials", (*PLUMBING_MATERIALS, *proposed.MATERIALS))
    item_types = Registry(
        "item_types",
        (*PLUMBING_ITEM_TYPES, *ELECTRICAL_ITEM_TYPES, *proposed.ITEM_TYPES),
    )
    # A family word that is also an item-type spelling would make "condulet"
    # resolvable two ways, which is the exact thing families exist to prevent.
    # Raised at import, like the duplicate-alias check, rather than discovered
    # when two totals disagree.
    for family_alias, family in _FAMILY_BY_ALIAS.items():
        clash = item_types.lookup(family_alias)
        if clash is not None:
            raise ValueError(
                f"family {family.code} alias {family_alias!r} is also the "
                f"item type {clash.code}: a family word must not be a line item"
            )
    return materials, item_types


MATERIALS, ITEM_TYPES = _build_registries()


# ---------------------------------------------------------------------------
# Resolution, rendering, keys
# ---------------------------------------------------------------------------


def _disambiguate(
    spelling: str,
    *,
    discipline: Discipline | None,
    prefer: TermKind | None,
) -> TermKind | None:
    """Decide what an ambiguous spelling meant, or refuse.

    Returns ``None`` when the spelling is not ambiguous at all (the common
    case), the settled :class:`TermKind` when the caller said enough, and
    raises :class:`AmbiguousTerm` when it did not. Never guesses.
    """
    key = alias_key(spelling)
    kinds = AMBIGUOUS_TERMS.get(key)
    if kinds is None:
        return None
    if prefer is not None:
        if prefer not in kinds:
            raise AmbiguousTerm(
                f"{spelling!r} means one of {[k.value for k in kinds]}; "
                f"prefer={prefer.value} is not one of them"
            )
        return prefer
    if discipline is not None:
        settled = _AMBIGUOUS_BY_DISCIPLINE.get(key, {}).get(discipline)
        if settled is not None:
            return settled
        raise AmbiguousTerm(
            f"{spelling!r} means one of {[k.value for k in kinds]} and "
            f"discipline={discipline.value} does not settle it; pass prefer="
        )
    raise AmbiguousTerm(
        f"{spelling!r} is ambiguous — it means one of "
        f"{[k.value for k in kinds]}. Pass discipline= or prefer=; this "
        f"vocabulary does not guess (LB the conduit body vs LB the pound)."
    )


def resolve_material(spelling: str) -> Material | None:
    """Any accepted spelling -> the one ``Material``. ``None`` if unknown."""
    return MATERIALS.lookup(spelling)  # type: ignore[return-value]


def resolve_item_type(
    spelling: str,
    *,
    discipline: Discipline | None = None,
    prefer: TermKind | None = None,
) -> ItemType | None:
    """Any accepted spelling -> the one ``ItemType``. ``None`` if unknown.

    Raises :class:`AmbiguousTerm` for a spelling in :data:`AMBIGUOUS_TERMS`
    unless ``discipline=`` or ``prefer=`` settles it — a bare ``"LB"`` read out
    of a *Unit* column must not quietly become a conduit body just because this
    function was the one that happened to be called.

    Raises :class:`UnderSpecifiedTerm` (a subclass, so the same ``except``
    catches both) for a **family** word such as ``"condulet"``. ``None`` here
    means "unknown word"; a family word is the opposite of unknown, and
    returning ``None`` for it left the caller to discover the difference as an
    ``AttributeError`` on ``.code`` at some unrelated line.
    """
    kind = _disambiguate(spelling, discipline=discipline, prefer=prefer)
    if kind is not None and kind is not TermKind.ITEM_TYPE:
        return None
    found = ITEM_TYPES.lookup(spelling)
    if found is not None:
        return found  # type: ignore[return-value]
    # Not an item type. Before saying "unknown", check whether it is a word we
    # know perfectly well and simply cannot turn into one line. ``_build_registries``
    # guarantees no spelling is both, so this order cannot mask an item type.
    family = resolve_family(spelling)
    if family is not None:
        raise UnderSpecifiedTerm(spelling, family, family_members(family))
    return None


def resolve_family(spelling: str) -> Family | None:
    """Any accepted spelling -> the one ``Family``. ``None`` if unknown.

    A family is never a line item; see :class:`Family`.
    """
    return _FAMILY_BY_ALIAS.get(alias_key(spelling))


def family_members(family: Family) -> tuple[ItemType, ...]:
    """Every ``ItemType`` in a family, in registry order.

    The review UI's answer to "the drawing says condulet — which one?" is this
    list, offered to a human. It is not a place to pick a default.
    """
    return tuple(
        e for e in ITEM_TYPES
        if isinstance(e, ItemType) and e.category is family.category
    )


#: Written unit spellings the estimator's world uses -> the enum member. ``LS``
#: is here because every estimator spreadsheet writes it and the enum member is
#: ``LOT`` (``docs/output-schema.md`` §6, item 6, still outstanding).
_UNIT_BY_ALIAS: Mapping[str, UnitOfMeasure] = {
    "EA": UnitOfMeasure.EACH,
    "EACH": UnitOfMeasure.EACH,
    "LF": UnitOfMeasure.LINEAR_FEET,
    "LINEARFEET": UnitOfMeasure.LINEAR_FEET,
    "SF": UnitOfMeasure.SQUARE_FEET,
    "CF": UnitOfMeasure.CUBIC_FEET,
    "LB": UnitOfMeasure.POUNDS,
    "LBS": UnitOfMeasure.POUNDS,
    "POUND": UnitOfMeasure.POUNDS,
    "POUNDS": UnitOfMeasure.POUNDS,
    "HR": UnitOfMeasure.HOURS,
    "LOT": UnitOfMeasure.LOT,
    "LS": UnitOfMeasure.LOT,
}


def resolve_unit(
    spelling: str,
    *,
    discipline: Discipline | None = None,
    prefer: TermKind | None = None,
) -> UnitOfMeasure | None:
    """A written unit -> ``UnitOfMeasure``. ``None`` if unknown.

    Same guard as :func:`resolve_item_type`, from the other side: a bare
    ``"LB"`` scraped off a legend or a fitting schedule does not become pounds
    without the caller saying it is a unit it is reading.
    """
    kind = _disambiguate(spelling, discipline=discipline, prefer=prefer)
    if kind is not None and kind is not TermKind.UNIT:
        return None
    return _UNIT_BY_ALIAS.get(alias_key(spelling))


def resolve_term(
    spelling: str,
    *,
    discipline: Discipline | None = None,
    prefer: TermKind | None = None,
) -> ItemType | Material | Family | UnitOfMeasure | None:
    """The one entry point for a string of unknown kind.

    Anything reading words off a drawing — a legend, a schedule header, a
    re-imported export — has a string and no type. This resolves it to whatever
    the vocabulary says it is, in a fixed order (item type, family, material,
    unit), and raises :class:`AmbiguousTerm` rather than choosing when the word
    has two meanings. ``None`` means the word is unknown, which is a review
    item, never a new line.
    """
    kind = _disambiguate(spelling, discipline=discipline, prefer=prefer)
    if kind is TermKind.UNIT:
        return _UNIT_BY_ALIAS.get(alias_key(spelling))
    if kind is TermKind.MATERIAL:
        return resolve_material(spelling)
    if kind is TermKind.FAMILY:
        return resolve_family(spelling)
    if kind is TermKind.ITEM_TYPE:
        return ITEM_TYPES.lookup(spelling)  # type: ignore[return-value]
    item_type = ITEM_TYPES.lookup(spelling)
    if item_type is not None:
        return item_type  # type: ignore[return-value]
    family = resolve_family(spelling)
    if family is not None:
        return family
    material = MATERIALS.lookup(spelling)
    if material is not None:
        return material  # type: ignore[return-value]
    return _UNIT_BY_ALIAS.get(alias_key(spelling))


def render_item_name(
    item_type: ItemType,
    *,
    material: Material | None = None,
    size: Size | None = None,
) -> str:
    """``{size} {material} {type}`` — the only way a name is ever produced."""
    parts = [
        size.display if size is not None else "",
        material.token if material is not None else "",
        item_type.token,
    ]
    return " ".join(p for p in parts if p)


def item_key(
    item_type: ItemType,
    *,
    discipline: Discipline,
    material: Material | None = None,
    size: Size | None = None,
) -> str:
    """The aggregation identity. Pipe-delimited, uppercase, no spaces lost.

    ``P|ELBOW_90|COPPER_WROT|0.5IN``. Note the material component: two
    identically-shaped fittings in different materials are different line
    items and different pricebook rows, so material is part of the key.
    ``aggregation_key()`` below is this key plus a scope, and that is what
    ``TakeoffLine.aggregation_key`` stores.

    A :class:`Family` is refused explicitly rather than duck-typed through:
    ``Family`` also has a ``code``, so without this check "condulet" would key
    as ``E|CONDUIT_BODY|-|-`` and sit in the export beside the LB total for the
    same physical fitting. Being a family *is* the refusal.
    """
    if not isinstance(item_type, ItemType):
        raise TypeError(
            f"item_key() takes an ItemType; got {type(item_type).__name__}. "
            "A family word (condulet) is under-specified and is a review item, "
            "not a line item — see Family."
        )
    return "|".join(
        (
            discipline.value,
            item_type.code,
            material.code if material is not None else "-",
            (size.key if size is not None and size.key else "-"),
        )
    )


#: The default scope component: the line rolls up the whole plan set.
DOC_SCOPE = "doc"


def sheet_scope(sheet_number: str) -> str:
    """The per-sheet scope component, for classes that must not roll up.

    Used for enlarged/partial plans and for anything on a sheet flagged as a
    possible double-count (``03-pipeline-specs.md`` §5.2).
    """
    return f"sheet:{sheet_number}"


def aggregation_key(
    item_type: ItemType,
    *,
    discipline: Discipline,
    material: Material | None = None,
    size: Size | None = None,
    scope: str = DOC_SCOPE,
) -> str:
    """``TakeoffLine.aggregation_key`` — the grouping identity for a line.

    ``item_key()`` plus a scope: ``P|ELBOW_90|COPPER_WROT|0.5IN|doc``.

    **Material is a component, and that is a correctness property, not a
    nicety.** Without it ``1/2" copper 90`` and ``1/2" PVC 90`` share a key,
    sum into a single total, and the estimator sees one plausible-looking
    number with no symptom of the merge. Items with no material take ``-``, so
    the key is always five fields wide and a missing material can never shift
    the meaning of a later field.

    The size component is ``Size.key``, not ``Size.display`` — the same
    normalised form stage A produces, so the key is stable against how the
    size happened to be written on the drawing.
    """
    return "|".join(
        (item_key(item_type, discipline=discipline, material=material, size=size), scope)
    )


@dataclass(frozen=True, slots=True)
class ParsedItemName:
    item_type: ItemType
    material: Material | None = None
    size: Size | None = None
    unparsed: tuple[str, ...] = field(default_factory=tuple)


def parse_item_name(name: str, *, prefer: SizeKind = SizeKind.NOMINAL) -> ParsedItemName | None:
    """Inverse of :func:`render_item_name`, for round-tripping an export.

    Greedy longest-suffix on the item type, then longest-prefix on the size,
    remainder is the material. Returns ``None`` when the item type cannot be
    resolved — an unresolvable name is a review item, never a silent new line.

    This deliberately does **not** apply the :data:`AMBIGUOUS_TERMS` guard: it
    parses the ``Description`` column, which by construction contains rendered
    item names and never a unit, so ``1/2" LB`` here is a conduit body with no
    ambiguity to resolve. The guard belongs on :func:`resolve_term` and friends,
    which are what read a loose string off a drawing or a spreadsheet cell.
    """
    words = str(name).split()
    if not words:
        return None

    item_type = None
    head: list[str] = []
    for take in range(len(words), 0, -1):
        candidate = ITEM_TYPES.lookup(" ".join(words[-take:]))
        if candidate is not None:
            item_type = candidate
            head = words[: len(words) - take]
            break
    if item_type is None:
        return None

    size: Size | None = None
    rest = head
    for take in range(len(head), 0, -1):
        parsed = parse_size(" ".join(head[:take]), prefer=prefer)
        if parsed is not None:
            size = parsed
            rest = head[take:]
            break

    material = MATERIALS.lookup(" ".join(rest)) if rest else None
    unparsed = () if (material is not None or not rest) else tuple(rest)
    return ParsedItemName(
        item_type=item_type,  # type: ignore[arg-type]
        material=material,  # type: ignore[arg-type]
        size=size,
        unparsed=unparsed,
    )


def pending_owner_confirmation() -> tuple[tuple[str, str, str], ...]:
    """Every ``(registry, code, trade)`` still awaiting the owner's wording.

    This is the list that goes in front of the owner. It is generated from the
    data so it cannot drift out of date with the vocabulary itself.
    """
    out: list[tuple[str, str, str]] = []
    sources: tuple[tuple[str, Iterable[Material | ItemType | Family]], ...] = (
        ("material", MATERIALS),
        ("item_type", ITEM_TYPES),
        ("family", FAMILIES),
    )
    for registry_name, registry in sources:
        for entry in registry:
            for trade, status in entry.trades:
                if status is VocabStatus.PROPOSED_PENDING_OWNER:
                    out.append((registry_name, entry.code, trade.value))
    return tuple(sorted(out))
