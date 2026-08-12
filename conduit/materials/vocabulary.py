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
* ``PROPOSED_PENDING_OWNER`` — **we made it up as a starting point.** The owner
  has not supplied electrical or HVAC wording, so the whole of
  ``conduit/materials/proposed.py`` carries this status. Do not treat any of it
  as agreed. ``pending_owner_confirmation()`` prints the list to be reviewed.

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
    "VOCABULARY_VERSION",
    "ITEM_TYPES",
    "MATERIALS",
    "ItemCategory",
    "ItemType",
    "Material",
    "ParsedItemName",
    "Registry",
    "VocabStatus",
    "alias_key",
    "default_uom",
    "item_key",
    "parse_item_name",
    "pending_owner_confirmation",
    "render_item_name",
    "resolve_item_type",
    "resolve_material",
]

#: Bump whenever a token, an alias or a key format changes. Written to
#: ``PipelineRun.model_versions["vocabulary"]`` so an old export stays
#: explicable after the words move.
VOCABULARY_VERSION = "vocab-1"


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
    WIRE = "wire"
    DEVICE = "device"
    DUCT = "duct"
    DUCT_FITTING = "duct_fitting"
    HANGER = "hanger"
    INSULATION = "insulation"
    ACCESSORY = "accessory"
    ALLOWANCE = "allowance"


#: Category -> unit. Linear things are LF, discrete things are EA, an allowance
#: is a lump sum. NOTE: the owner's workbook writes ``LS`` for lump sum and
#: ``UnitOfMeasure`` has no ``LS`` member — it has ``LOT``. See
#: ``docs/output-schema.md`` §6; the enum member is listed as missing and is
#: not added in this PR.
_CATEGORY_UOM: Mapping[ItemCategory, UnitOfMeasure] = {
    ItemCategory.PIPE: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.CONDUIT: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.WIRE: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.DUCT: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.INSULATION: UnitOfMeasure.LINEAR_FEET,
    ItemCategory.FITTING: UnitOfMeasure.EACH,
    ItemCategory.DUCT_FITTING: UnitOfMeasure.EACH,
    ItemCategory.VALVE: UnitOfMeasure.EACH,
    ItemCategory.FIXTURE: UnitOfMeasure.EACH,
    ItemCategory.EQUIPMENT: UnitOfMeasure.EACH,
    ItemCategory.DEVICE: UnitOfMeasure.EACH,
    ItemCategory.HANGER: UnitOfMeasure.EACH,
    ItemCategory.ACCESSORY: UnitOfMeasure.EACH,
    ItemCategory.ALLOWANCE: UnitOfMeasure.LOT,
}


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
    ItemType("COUPLING", "coupling", ItemCategory.FITTING,
             ((_P, _OWNER), (_E, _PROPOSED)), aliases=("cplg", "coup")),
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


def _build_registries() -> tuple[Registry, Registry]:
    # Imported here rather than at module top so the proposed vocabulary is
    # visibly a separate, swappable input rather than part of this file.
    from conduit.materials import proposed

    materials = Registry("materials", (*PLUMBING_MATERIALS, *proposed.MATERIALS))
    item_types = Registry("item_types", (*PLUMBING_ITEM_TYPES, *proposed.ITEM_TYPES))
    return materials, item_types


MATERIALS, ITEM_TYPES = _build_registries()


# ---------------------------------------------------------------------------
# Resolution, rendering, keys
# ---------------------------------------------------------------------------


def resolve_material(spelling: str) -> Material | None:
    """Any accepted spelling -> the one ``Material``. ``None`` if unknown."""
    return MATERIALS.lookup(spelling)  # type: ignore[return-value]


def resolve_item_type(spelling: str) -> ItemType | None:
    return ITEM_TYPES.lookup(spelling)  # type: ignore[return-value]


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
    """
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
    for registry_name, registry in (("material", MATERIALS), ("item_type", ITEM_TYPES)):
        for entry in registry:
            for trade, status in entry.trades:
                if status is VocabStatus.PROPOSED_PENDING_OWNER:
                    out.append((registry_name, entry.code, trade.value))
    return tuple(sorted(out))
