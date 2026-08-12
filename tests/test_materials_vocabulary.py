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
    AMBIGUOUS_TERMS,
    CONDUIT_BODY_FAMILY,
    FAMILIES,
    ITEM_TYPES,
    MATERIALS,
    VOCABULARY_VERSION,
    WEIGHT_CATEGORIES,
    AmbiguousTerm,
    Family,
    ItemCategory,
    ItemType,
    Material,
    Registry,
    TermKind,
    UnderSpecifiedTerm,
    VocabStatus,
    alias_key,
    default_uom,
    family_members,
    item_key,
    parse_item_name,
    pending_owner_confirmation,
    render_item_name,
    resolve_family,
    resolve_item_type,
    resolve_material,
    resolve_term,
    resolve_unit,
)

P = Discipline.PLUMBING
E = Discipline.ELECTRICAL
M = Discipline.MECHANICAL

#: The four words the owner confirmed, and where each one lives.
OWNER_CONFIRMED_ELECTRICAL = ("CONDULET_LB", "CONNECTOR", "COUPLING")


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
    assert resolve_item_type("conduit").uom is UnitOfMeasure.LINEAR_FEET
    assert resolve_item_type("90").uom is UnitOfMeasure.EACH
    assert resolve_item_type("ball valve").uom is UnitOfMeasure.EACH
    assert resolve_item_type("hanger").uom is UnitOfMeasure.EACH
    assert resolve_item_type("connector").uom is UnitOfMeasure.EACH


def test_fabricated_duct_is_pounds_and_flex_duct_is_not() -> None:
    """Owner-directed: "duct gets taken off by the pound"."""
    assert resolve_item_type("duct").uom is UnitOfMeasure.POUNDS
    assert resolve_item_type("ductwork").uom is UnitOfMeasure.POUNDS
    # Flex duct comes off a roll by the foot; weighing it would be an invention.
    assert resolve_item_type("flex duct").uom is UnitOfMeasure.LINEAR_FEET
    # A duct *fitting* is still a count. Its metal is in the duct pounds line
    # (docs/derived-quantities.md §6.6) and the two are never summed: different
    # units, different aggregation keys.
    assert resolve_item_type("volume damper").uom is UnitOfMeasure.EACH
    assert resolve_item_type("radius elbow").uom is UnitOfMeasure.EACH
    assert WEIGHT_CATEGORIES == frozenset({ItemCategory.DUCT})


def test_conduit_bodies_are_each() -> None:
    for spelling in ("LB", "LL", "LR", "T condulet", "C condulet"):
        entry = resolve_item_type(spelling, discipline=E)
        assert entry.category is ItemCategory.CONDUIT_BODY, spelling
        assert entry.uom is UnitOfMeasure.EACH, spelling


def test_the_unit_is_a_property_of_the_category_never_of_the_line() -> None:
    """No item type may carry a unit of its own; it reads one off its category.

    This is the property that makes "duct is pounds" a one-line change rather
    than a rule every writer has to remember.
    """
    for entry in ITEM_TYPES:
        assert entry.uom is default_uom(entry.category), entry.code
    assert not hasattr(ItemType, "uom_override")
    # And every category resolves, including the two added for duct-by-weight.
    for category in ItemCategory:
        assert isinstance(default_uom(category), UnitOfMeasure), category


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


def test_electrical_and_hvac_words_are_unconfirmed_except_the_four_owner_gave() -> None:
    """The complement of the owner's answer, asserted so it cannot drift.

    Previously this asserted *every* E/M word was proposed. The owner has now
    named four, so the assertion narrows to exactly the rest — and any future
    promotion has to come with an edit here, which is the prompt to check that
    the owner really said it.
    """
    for trade in (E, M):
        for entry in ITEM_TYPES.for_trade(trade):
            expected = (
                VocabStatus.OWNER_SOURCED
                if (trade is E and entry.code in OWNER_CONFIRMED_ELECTRICAL)
                else VocabStatus.PROPOSED_PENDING_OWNER
            )
            assert entry.status_for(trade) is expected, entry.code


# --- the owner's confirmed electrical words --------------------------------


def test_the_four_confirmed_words_resolve_and_are_owner_sourced() -> None:
    """*"the electrical fitting words my estimators use — LB, condulet,
    coupling, connector."*"""
    assert resolve_item_type("LB", discipline=E).code == "CONDULET_LB"
    assert resolve_item_type("connector").code == "CONNECTOR"
    assert resolve_item_type("coupling").code == "COUPLING"
    assert resolve_family("condulet").code == "CONDUIT_BODY"

    for code in OWNER_CONFIRMED_ELECTRICAL:
        assert ITEM_TYPES.get(code).status_for(E) is VocabStatus.OWNER_SOURCED, code
    assert CONDUIT_BODY_FAMILY.status_for(E) is VocabStatus.OWNER_SOURCED
    # coupling stays one entry shared with plumbing: one word, one key.
    assert ITEM_TYPES.get("COUPLING").status_for(P) is VocabStatus.OWNER_SOURCED


def test_confirmed_words_have_left_the_proposed_file() -> None:
    """``proposed.py`` is meant to be deleted wholesale; decisions cannot sit
    in it or they go with it."""
    proposed_codes = {e.code for e in (*proposed.MATERIALS, *proposed.ITEM_TYPES)}
    for code in OWNER_CONFIRMED_ELECTRICAL:
        assert code not in proposed_codes, code
    assert "COUPLING" not in proposed.SHARED_ENTRIES_ALSO_PROPOSED


def test_none_of_the_confirmed_words_is_still_pending() -> None:
    pending = {(code, trade) for _, code, trade in pending_owner_confirmation()}
    for code in OWNER_CONFIRMED_ELECTRICAL:
        assert (code, E.value) not in pending, code
    assert ("CONDUIT_BODY", E.value) not in pending
    # ...and the rest of electrical still is.
    assert ("CONDULET_LL", E.value) in pending


# --- LB is a condulet: family and member, never siblings -------------------


def test_condulet_is_the_family_and_lb_is_a_member_of_it() -> None:
    family = resolve_family("condulet")
    assert isinstance(family, Family)
    assert family.category is ItemCategory.CONDUIT_BODY
    lb = resolve_item_type("LB", discipline=E)
    assert lb.category is family.category
    assert lb in family_members(family)
    # Every condulet letter belongs to the same family, so "which condulet?"
    # has a complete answer to offer a reviewer.
    assert {m.code for m in family_members(family)} == {
        "CONDULET_LB", "CONDULET_LL", "CONDULET_LR", "CONDULET_T", "CONDULET_C",
    }


@pytest.mark.parametrize("spelling", ["condulet", "condulets", "conduit body",
                                      "Conduit Bodies", "CONDULET"])
def test_the_family_word_resolves_but_never_as_a_line_item(spelling: str) -> None:
    assert resolve_family(spelling) is not None, spelling
    # Known word, so not silently unknown...
    assert resolve_term(spelling) is resolve_family(spelling)
    # ...but not an item type, so it cannot become a line — and asking for it as
    # one fails out loud rather than returning the None that means "unknown".
    with pytest.raises(UnderSpecifiedTerm):
        resolve_item_type(spelling)


def test_condulet_and_lb_cannot_produce_two_totals_for_one_fitting() -> None:
    """The property the whole family/member split exists for.

    An LB *is* a condulet. If both words could open an aggregation key, one
    physical fitting would sit in the export twice — the same defect as the
    material-blind key PR #4 fixed, one column over.
    """
    lb = resolve_item_type("LB", discipline=E)
    lb_key = item_key(lb, discipline=E)
    assert lb_key == "E|CONDULET_LB|-|-"

    family = resolve_family("condulet")
    # A Family has a .code, so duck typing would have let it through. It does
    # not: item_key refuses anything that is not an ItemType.
    with pytest.raises(TypeError, match="review item"):
        item_key(family, discipline=E)  # type: ignore[arg-type]

    # And no spelling of the family word reaches an item type by another route.
    for spelling in (family.code, family.token, *family.aliases):
        with pytest.raises(UnderSpecifiedTerm):
            resolve_item_type(spelling)
        assert ITEM_TYPES.lookup(spelling) is None, spelling

    # Every spelling of the *member* lands on that one key, as usual.
    keys = {
        item_key(resolve_item_type(s, discipline=E), discipline=E)
        for s in ("LB", "lb condulet", "condulet LB", "type LB", "LB body")
    }
    assert keys == {lb_key}


def test_a_family_word_may_not_also_be_an_item_type_spelling() -> None:
    """Enforced at import time; asserted here so the reason is written down."""
    for family in FAMILIES:
        for spelling in (family.code, family.token, *family.aliases):
            assert ITEM_TYPES.lookup(spelling) is None, spelling


# --- a known word must never fail as an AttributeError somewhere else -------


def test_a_family_word_fails_explicitly_rather_than_returning_none() -> None:
    """``resolve_item_type("condulet")`` used to return ``None``.

    ``None`` is this module's answer for "never heard that word", so a caller
    doing ``.code`` on it got ``AttributeError: 'NoneType' object has no
    attribute 'code'`` at some line that no longer had the string in hand.
    "Condulet" is a word the owner named and an estimator says; it must fail as
    a typed, explicit refusal at the point of the lookup.
    """
    with pytest.raises(UnderSpecifiedTerm) as excinfo:
        resolve_item_type("condulet")

    err = excinfo.value
    # It carries what the review UI needs to ask a human "which condulet?".
    assert err.term == "condulet"
    assert err.family is resolve_family("condulet")
    assert {m.code for m in err.candidates} == {
        "CONDULET_LB", "CONDULET_LL", "CONDULET_LR", "CONDULET_T", "CONDULET_C",
    }
    # And the message names the family and the candidates, not just "no".
    assert "CONDUIT_BODY" in str(err)
    assert "CONDULET_LB" in str(err)


def test_the_family_refusal_is_the_same_error_type_the_lb_refusal_raises() -> None:
    """One ``except`` catches both senses of "did not resolve to one thing".

    The two failures are still distinguishable, because they are not the same
    problem: ``"LB"`` is settleable by the caller (pass ``discipline=``),
    ``"condulet"`` is not settleable by anyone but a human reading the drawing.
    """
    assert issubclass(UnderSpecifiedTerm, AmbiguousTerm)

    for spelling in ("LB", "condulet"):
        with pytest.raises(AmbiguousTerm):
            resolve_item_type(spelling)

    # The narrower type is only raised for the unsettleable one.
    with pytest.raises(UnderSpecifiedTerm):
        resolve_item_type("condulet")
    try:
        resolve_item_type("LB")
    except AmbiguousTerm as exc:
        assert not isinstance(exc, UnderSpecifiedTerm)
    # ...and supplying the discipline settles LB, which no argument does for
    # condulet: there is no discipline or prefer= that names one conduit body.
    assert resolve_item_type("LB", discipline=E).code == "CONDULET_LB"
    for kwargs in ({"discipline": E}, {"prefer": TermKind.ITEM_TYPE},
                   {"prefer": TermKind.FAMILY}):
        with pytest.raises(UnderSpecifiedTerm):
            resolve_item_type("condulet", **kwargs)  # type: ignore[arg-type]


def test_no_word_this_vocabulary_knows_returns_none_from_resolve_item_type() -> None:
    """The general property, so the next family word inherits the fix.

    Every spelling the vocabulary knows either resolves to exactly one thing or
    refuses out loud. ``None`` is reserved for words we genuinely do not know.
    """
    known: list[str] = []
    for entry in ITEM_TYPES:
        known.extend((entry.code, entry.token, *entry.aliases))
    for family in FAMILIES:
        known.extend((family.code, family.token, *family.aliases))

    for spelling in known:
        try:
            resolved = resolve_item_type(spelling, discipline=E)
        except AmbiguousTerm:
            continue  # Explicit refusal: allowed.
        assert resolved is not None, f"{spelling!r} resolved to None silently"

    # Contrast: a word we really do not know is still a quiet None, because that
    # is a review item and not an error.
    assert resolve_item_type("frobnicator") is None


# --- LB the conduit body vs LB the pound -----------------------------------


def test_lb_is_registered_as_ambiguous() -> None:
    assert AMBIGUOUS_TERMS[alias_key("LB")] == (TermKind.ITEM_TYPE, TermKind.UNIT)


@pytest.mark.parametrize("resolver", [resolve_item_type, resolve_unit, resolve_term])
@pytest.mark.parametrize("spelling", ["LB", "lb", " Lb "])
def test_a_bare_lb_refuses_to_resolve_at_all(resolver, spelling: str) -> None:
    """Neither meaning wins by being the one the caller happened to ask for."""
    with pytest.raises(AmbiguousTerm):
        resolver(spelling)


def test_discipline_settles_lb_and_settles_it_the_same_way_everywhere() -> None:
    assert resolve_item_type("LB", discipline=E).code == "CONDULET_LB"
    assert resolve_term("LB", discipline=E).code == "CONDULET_LB"
    assert resolve_unit("LB", discipline=M) is UnitOfMeasure.POUNDS
    assert resolve_term("LB", discipline=M) is UnitOfMeasure.POUNDS
    # Asking the wrong resolver with a settling discipline gets nothing, not
    # the other meaning.
    assert resolve_unit("LB", discipline=E) is None
    assert resolve_item_type("LB", discipline=M) is None


def test_a_discipline_that_does_not_settle_lb_still_refuses() -> None:
    """Plumbing has no conduit bodies and weighs nothing. It is not a tiebreak."""
    with pytest.raises(AmbiguousTerm, match="does not settle it"):
        resolve_term("LB", discipline=P)


def test_prefer_settles_lb_explicitly() -> None:
    assert resolve_term("LB", prefer=TermKind.ITEM_TYPE).code == "CONDULET_LB"
    assert resolve_term("LB", prefer=TermKind.UNIT) is UnitOfMeasure.POUNDS
    with pytest.raises(AmbiguousTerm, match="not one of them"):
        resolve_term("LB", prefer=TermKind.MATERIAL)


def test_the_two_lbs_are_structurally_separate_not_just_guarded() -> None:
    """The guard is the second line of defence. This is the first.

    A conduit body enters a key as ``CONDULET_LB``; a pound enters the export
    as a ``UnitOfMeasure`` in a different column. No string is shared, so no
    aggregation can merge duct weight with conduit-body counts even if a
    resolver were bypassed entirely.
    """
    unit_values = {u.value for u in UnitOfMeasure}
    for entry in ITEM_TYPES:
        assert entry.code not in unit_values, entry.code
    lb = ITEM_TYPES.get("CONDULET_LB")
    assert lb.token == "LB" and lb.code != "LB"
    assert "|LB|" not in f"|{item_key(lb, discipline=E)}|"
    # A conduit body is counted; only fabricated duct is weighed.
    assert lb.uom is UnitOfMeasure.EACH
    assert resolve_item_type("duct").uom is UnitOfMeasure.POUNDS


def test_an_lb_inside_a_rendered_name_is_still_a_conduit_body() -> None:
    """``parse_item_name`` reads the Description column, which never holds a
    unit — so the ambiguity does not arise there and is not guarded there."""
    name = render_item_name(ITEM_TYPES.get("CONDULET_LB"), size=parse_size('3/4"'))
    assert name == '3/4" LB'
    parsed = parse_item_name(name)
    assert parsed.item_type.code == "CONDULET_LB"
    assert parsed.size.key == parse_size('3/4"').key


def test_unit_spellings_that_are_not_ambiguous_resolve_plainly() -> None:
    assert resolve_unit("EA") is UnitOfMeasure.EACH
    assert resolve_unit("LF") is UnitOfMeasure.LINEAR_FEET
    assert resolve_unit("lbs") is UnitOfMeasure.POUNDS
    assert resolve_unit("pounds") is UnitOfMeasure.POUNDS
    # The workbook writes LS; the enum member is LOT (output-schema §6 item 6).
    assert resolve_unit("LS") is UnitOfMeasure.LOT
    assert resolve_unit("furlong") is None


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
