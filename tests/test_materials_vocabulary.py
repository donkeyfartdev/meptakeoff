"""Vocabulary tests.

Three properties are being defended:

1. **Rendering is deterministic** — the same components always produce the same
   string, and it is the string the owner asked for (``1/2" copper 90``).
2. **Round trip** — a rendered name parses back to the components it came from,
   so an export can be read back without a lookup table.
3. **Spellings collapse** — every accepted way of writing a fitting lands on
   one key, which is the only reason totals do not fragment.
"""

from __future__ import annotations

import pytest

from conduit.db.models import Discipline, UnitOfMeasure
from conduit.materials import proposed
from conduit.materials.sizes import Size, SizeKind, parse_size
from conduit.materials.vocabulary import (
    ITEM_TYPES,
    MATERIALS,
    VOCABULARY_VERSION,
    ItemCategory,
    ItemType,
    Material,
    Registry,
    VocabStatus,
    alias_key,
    default_uom,
    item_key,
    parse_item_name,
    pending_owner_confirmation,
    render_item_name,
    resolve_item_type,
    resolve_material,
)

P = Discipline.PLUMBING


def build(size: str | None, material: str, item_type: str) -> str:
    return render_item_name(
        resolve_item_type(item_type),
        material=resolve_material(material),
        size=parse_size(size) if size else None,
    )


# --- 1. rendering ----------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "material", "item_type", "expected"),
    [
        ('1/2"', "copper", "90", '1/2" copper 90'),
        ('3/4"', "copper", "tee", '3/4" copper tee'),
        ('2"', "PVC", "45", '2" PVC 45'),
        ('4"', "PVC DWV", "wye", '4" PVC DWV wye'),
        ('1/2"', "Type L copper", "tube", '1/2" Type L copper tube'),
        ('1"', "lead-free brass", "ball valve", '1" lead-free brass ball valve'),
    ],
)
def test_renders_the_owner_shorthand(size, material, item_type, expected) -> None:
    assert build(size, material, item_type) == expected


def test_rendering_omits_absent_components() -> None:
    recirc = resolve_item_type("recirculation pump")
    assert render_item_name(recirc) == "recirculation pump"
    assert render_item_name(recirc, size=Size.label("1/25 HP")) == "1/25 HP recirculation pump"


def test_rendering_is_stable_across_calls() -> None:
    first = build('1-1/4"', "copper", "coupling")
    for _ in range(5):
        assert build('1-1/4"', "copper", "coupling") == first
    assert first == '1-1/4" copper coupling'


# --- 2. round trip ---------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "material", "item_type"),
    [
        ('1/2"', "copper", "90"),
        ('3/4"', "copper", "tee"),
        ('2"', "PVC", "45"),
        ('4"', "no-hub CI", "no-hub coupling"),
        ('3/4" x 1/2"', "copper", "reducer"),
        ('1-1/2"', "bronze", "PRV"),
        ('#12 AWG', "THHN copper", "wire"),
        ('3/4"', "EMT", "conduit"),
    ],
)
def test_round_trip(size, material, item_type) -> None:
    name = build(size, material, item_type)
    parsed = parse_item_name(name)
    assert parsed is not None, name
    assert parsed.item_type.code == resolve_item_type(item_type).code
    assert parsed.material.code == resolve_material(material).code
    assert parsed.size.key == parse_size(size).key
    assert parsed.unparsed == ()
    assert render_item_name(
        parsed.item_type, material=parsed.material, size=parsed.size
    ) == name


def test_round_trip_of_a_rectangular_duct_name() -> None:
    duct = resolve_item_type("duct")
    name = render_item_name(duct, material=resolve_material("galv steel"),
                            size=Size.rectangular(12, 8))
    assert name == "12x8 galv steel duct"
    parsed = parse_item_name(name, prefer=SizeKind.RECTANGULAR)
    assert parsed.size == Size.rectangular(12, 8)
    assert parsed.item_type.code == "DUCT"


def test_unresolvable_name_returns_none_rather_than_inventing_a_line() -> None:
    assert parse_item_name("1/2\" copper widget") is None
    assert parse_item_name("") is None


def test_unknown_material_is_reported_not_swallowed() -> None:
    parsed = parse_item_name('1/2" unobtanium 90')
    assert parsed is not None
    assert parsed.material is None
    assert parsed.unparsed == ("unobtanium",)


# --- 3. spellings collapse -------------------------------------------------


@pytest.mark.parametrize(
    ("code", "spellings"),
    [
        ("ELBOW_90", ["90", "ell", "ELL", "elbow", "90 deg", "1/4 bend", "90 ELL"]),
        ("ELBOW_45", ["45", "45 ell", "1/8 bend", "eighth bend"]),
        ("TEE", ["tee", "T", "TEE"]),
        ("COUPLING", ["coupling", "cplg", "COUP"]),
        ("BACKFLOW_RPZ", ["RPZ", "rpz", "RPZ backflow preventer"]),
        ("WATER_CLOSET", ["water closet", "WC", "toilet"]),
    ],
)
def test_item_type_spellings_collapse(code: str, spellings: list[str]) -> None:
    assert {resolve_item_type(s).code for s in spellings} == {code}


@pytest.mark.parametrize(
    ("code", "spellings"),
    [
        ("COPPER_WROT", ["copper", "CU", "wrot copper", "wrought copper"]),
        ("COPPER_TYPE_L", ["Type L copper", "type l cu", "L COPPER"]),
        ("PVC_SCH40", ["PVC", "pvc sch 40", "Schedule 40 PVC"]),
        ("CAST_IRON_NO_HUB", ["no-hub CI", "no hub cast iron", "NH CI"]),
    ],
)
def test_material_spellings_collapse(code: str, spellings: list[str]) -> None:
    assert {resolve_material(s).code for s in spellings} == {code}


def test_three_spellings_of_one_fitting_produce_one_key() -> None:
    """The headline property: totals cannot fragment across spellings."""
    keys = set()
    for name in ['1/2" copper 90', "1/2 CU ELL", '.5" wrought copper elbow']:
        parsed = parse_item_name(name)
        assert parsed is not None, name
        keys.add(item_key(parsed.item_type, discipline=P,
                          material=parsed.material, size=parsed.size))
    assert keys == {"P|ELBOW_90|COPPER_WROT|0.5IN"}


def test_material_is_part_of_the_key() -> None:
    """A copper 90 and a PVC 90 are different pricebook rows."""
    copper = item_key(resolve_item_type("90"), discipline=P,
                      material=resolve_material("copper"), size=parse_size('2"'))
    pvc = item_key(resolve_item_type("90"), discipline=P,
                   material=resolve_material("PVC"), size=parse_size('2"'))
    assert copper != pvc
    assert copper == "P|ELBOW_90|COPPER_WROT|2IN"


def test_key_uses_a_placeholder_rather_than_dropping_a_component() -> None:
    key = item_key(resolve_item_type("allowance"), discipline=P)
    assert key == "P|ALLOWANCE|-|-"
    assert key.count("|") == 3


# --- registry integrity ----------------------------------------------------


def test_duplicate_alias_is_a_hard_error() -> None:
    entries = [
        Material("A", "alpha", ((P, VocabStatus.TRADE_STANDARD),), aliases=("x",)),
        Material("B", "beta", ((P, VocabStatus.TRADE_STANDARD),), aliases=("X",)),
    ]
    with pytest.raises(ValueError, match="claimed by both"):
        Registry("t", entries)


def test_duplicate_code_is_a_hard_error() -> None:
    entries = [
        Material("A", "alpha", ((P, VocabStatus.TRADE_STANDARD),)),
        Material("A", "gamma", ((P, VocabStatus.TRADE_STANDARD),)),
    ]
    with pytest.raises(ValueError, match="duplicate code"):
        Registry("t", entries)


def test_every_entry_has_a_token_and_at_least_one_trade() -> None:
    for registry in (MATERIALS, ITEM_TYPES):
        for entry in registry:
            assert entry.token.strip(), entry.code
            assert entry.trades, entry.code
            assert alias_key(entry.token), entry.code


def test_every_category_has_a_unit_and_every_item_type_resolves_one() -> None:
    for category in ItemCategory:
        assert isinstance(default_uom(category), UnitOfMeasure)
    for entry in ITEM_TYPES:
        assert isinstance(entry.uom, UnitOfMeasure)


def test_linear_items_are_lf_and_discrete_items_are_ea() -> None:
    assert resolve_item_type("pipe").uom is UnitOfMeasure.LINEAR_FEET
    assert resolve_item_type("insulation").uom is UnitOfMeasure.LINEAR_FEET
    assert resolve_item_type("wire").uom is UnitOfMeasure.LINEAR_FEET
    assert resolve_item_type("90").uom is UnitOfMeasure.EACH
    assert resolve_item_type("ball valve").uom is UnitOfMeasure.EACH
    assert resolve_item_type("hanger").uom is UnitOfMeasure.EACH


# --- proposed vocabulary is labelled as such -------------------------------


def test_every_proposed_entry_is_marked_proposed_for_every_trade_it_claims() -> None:
    for entry in (*proposed.MATERIALS, *proposed.ITEM_TYPES):
        for trade, status in entry.trades:
            assert status is VocabStatus.PROPOSED_PENDING_OWNER, (entry.code, trade)


def test_proposed_entries_are_electrical_or_mechanical_only() -> None:
    for entry in (*proposed.MATERIALS, *proposed.ITEM_TYPES):
        assert set(entry.disciplines) <= {Discipline.ELECTRICAL, Discipline.MECHANICAL}, entry.code


def test_pending_list_contains_every_proposed_code() -> None:
    pending = {code for _, code, _ in pending_owner_confirmation()}
    for entry in (*proposed.MATERIALS, *proposed.ITEM_TYPES):
        assert entry.code in pending, entry.code
    # And the shared plumbing-sourced words that are also offered to E/M.
    for code in proposed.SHARED_ENTRIES_ALSO_PROPOSED:
        assert code in pending, code


def test_owner_sourced_plumbing_is_not_in_the_pending_list_for_plumbing() -> None:
    pending_p = {code for _, code, trade in pending_owner_confirmation() if trade == P.value}
    assert pending_p == set()
    assert resolve_item_type("water closet").status_for(P) is VocabStatus.OWNER_SOURCED
    assert resolve_material("Type L copper").status_for(P) is VocabStatus.OWNER_SOURCED


def test_electrical_and_hvac_fitting_words_are_all_still_unconfirmed() -> None:
    """Fails the day the owner's real wording lands — which is the point."""
    for trade in (Discipline.ELECTRICAL, Discipline.MECHANICAL):
        for entry in ITEM_TYPES.for_trade(trade):
            assert entry.status_for(trade) is VocabStatus.PROPOSED_PENDING_OWNER, entry.code


def test_vocabulary_version_is_recorded() -> None:
    assert VOCABULARY_VERSION and isinstance(VOCABULARY_VERSION, str)


def test_registry_lookup_of_an_unknown_word_is_none_not_an_exception() -> None:
    assert resolve_item_type("zorb") is None
    assert resolve_material("zorb") is None
    assert isinstance(ITEM_TYPES.get("ELBOW_90"), ItemType)
    assert isinstance(MATERIALS.get("COPPER_WROT"), Material)


def test_the_docs_pending_count_matches_the_registry() -> None:
    """``docs/output-schema.md`` §2.3 quotes a number. Keep it true.

    A design doc drifts from its code within days; the number of unconfirmed
    words is exactly the sort of figure that goes stale silently and then gets
    quoted at the owner. So the doc's claim is asserted against the registry
    rather than trusted.
    """
    import re
    from pathlib import Path

    doc = Path(__file__).resolve().parents[1] / "docs" / "output-schema.md"
    match = re.search(r"\*\*(\d+) \(entry, trade\) pairs pending", doc.read_text())
    assert match, "the pending-count sentence has moved; update this test with it"
    assert int(match.group(1)) == len(pending_owner_confirmation())
