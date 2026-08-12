"""Stage B rules, as pure functions (``03-pipeline-specs.md`` §1.2, §1.3, §1.7).

No database and no PDF here: these are the decisions that turn a string into a
discipline, and they are the part of stage B a reviewer will argue with.
"""

from __future__ import annotations

import pytest

from conduit.classify.rules import (
    classification_confidence,
    discipline_for_prefix,
    parse_sheet_number,
    sheet_number_candidates,
    subtype_for_title,
    title_discipline_votes,
)
from conduit.db.models import ClassificationMethod, Discipline, SheetSubtype


@pytest.mark.parametrize(
    ("text", "prefix", "number", "sub", "suffix"),
    [
        ("E-101", "E", "101", None, None),
        ("E101", "E", "101", None, None),
        ("M-101.1", "M", "101", "1", None),
        ("FP-2A", "FP", "2", None, "A"),
        ("P.201", "P", "201", None, None),
    ],
)
def test_sheet_number_regex_parses_the_conventional_forms(
    text, prefix, number, sub, suffix
) -> None:
    parsed = parse_sheet_number(text)
    assert parsed is not None
    assert (parsed.prefix, parsed.number, parsed.sub, parsed.suffix) == (
        prefix,
        number,
        sub,
        suffix,
    )


@pytest.mark.parametrize(
    "text",
    [
        "LEVEL 1",
        "24'-6\"",
        "TYPE A (4)",
        "SCALE: 1/8",
        "ELECTRICAL - LIGHTING PLAN - LEVEL 1",
        "1",
        "",
    ],
)
def test_sheet_number_regex_does_not_match_ordinary_sheet_text(text) -> None:
    assert all(parse_sheet_number(c) is None for c in sheet_number_candidates(text))


def test_a_labelled_sheet_number_is_still_found() -> None:
    """§1.2 assumes the number is its own span; real title blocks label it."""
    candidates = sheet_number_candidates("SHEET NUMBER: M-102")
    matches = [parse_sheet_number(c) for c in candidates]
    found = [m for m in matches if m is not None]
    assert found and found[0].normalised == "M-102"


@pytest.mark.parametrize(
    ("prefix", "discipline"),
    [
        ("E", Discipline.ELECTRICAL),
        ("FA", Discipline.ELECTRICAL),
        ("P", Discipline.PLUMBING),
        ("H", Discipline.MECHANICAL),
        ("FP", Discipline.FIRE_PROTECTION),
        ("S", Discipline.STRUCTURAL),
        ("G", Discipline.GENERAL),
        ("ZZ", Discipline.UNKNOWN),
        ("Q", Discipline.UNKNOWN),
    ],
)
def test_prefix_maps_to_discipline_and_unknown_is_not_a_guess(prefix, discipline) -> None:
    assert discipline_for_prefix(prefix) is discipline


@pytest.mark.parametrize(
    ("discipline", "title", "subtype"),
    [
        (Discipline.ELECTRICAL, "ELECTRICAL - LIGHTING PLAN", SheetSubtype.E_LIGHTING),
        (Discipline.ELECTRICAL, "ELECTRICAL - PANEL SCHEDULES", SheetSubtype.E_SCHEDULE),
        (Discipline.ELECTRICAL, "SYMBOL LEGEND", SheetSubtype.LEGEND),
        (Discipline.ELECTRICAL, "ENLARGED DETAIL - RISER", SheetSubtype.DETAIL),
        (Discipline.MECHANICAL, "MECHANICAL - DUCTWORK PLAN", SheetSubtype.M_DUCT),
        (Discipline.MECHANICAL, "MECHANICAL - EQUIPMENT SCHEDULE", SheetSubtype.M_SCHEDULE),
        (Discipline.PLUMBING, "PLUMBING - WASTE AND VENT PLAN", SheetSubtype.P_SANITARY),
        (Discipline.PLUMBING, "PLUMBING - DOMESTIC WATER PLAN", SheetSubtype.P_DOMESTIC_WATER),
        (Discipline.FIRE_PROTECTION, "SPRINKLER PLAN", SheetSubtype.FP_SPRINKLER),
        (Discipline.MECHANICAL, "MECHANICAL - HVAC PLAN - LEVEL 1", SheetSubtype.OTHER),
        (Discipline.UNKNOWN, "SOMETHING ELSE ENTIRELY", SheetSubtype.OTHER),
    ],
)
def test_subtype_keyword_vote_is_ordered_and_scoped(discipline, title, subtype) -> None:
    assert subtype_for_title(discipline, title)[0] is subtype


def test_short_keywords_do_not_swallow_longer_words() -> None:
    """``FA`` must not match ``FAN``; ``BAS`` must not match ``BASEMENT``."""
    assert subtype_for_title(Discipline.ELECTRICAL, "FAN POWER PLAN")[0] is SheetSubtype.E_POWER
    assert (
        subtype_for_title(Discipline.MECHANICAL, "BASEMENT DUCTWORK")[0] is SheetSubtype.M_DUCT
    )


def test_stems_match_their_plurals_and_compounds() -> None:
    assert subtype_for_title(Discipline.ELECTRICAL, "PANEL SCHEDULES")[0] is (
        SheetSubtype.E_SCHEDULE
    )
    assert subtype_for_title(Discipline.MECHANICAL, "DUCTWORK PLAN")[0] is SheetSubtype.M_DUCT


def test_title_votes_are_only_corroboration() -> None:
    votes = title_discipline_votes("PLUMBING - DOMESTIC WATER PLAN")
    assert votes == {Discipline.PLUMBING}
    assert title_discipline_votes(None) == set()
    assert title_discipline_votes("SHEET 3 OF 12") == set()


def test_confidence_is_the_specs_arithmetic() -> None:
    high = classification_confidence(
        ClassificationMethod.SHEET_NUMBER_REGEX,
        title_agrees=True,
        from_index=False,
        subtype_matched=True,
    )
    assert high == pytest.approx(0.95)

    disagreeing = classification_confidence(
        ClassificationMethod.SHEET_NUMBER_REGEX,
        title_agrees=False,
        from_index=False,
        subtype_matched=True,
    )
    assert disagreeing == pytest.approx(0.60)

    no_subtype = classification_confidence(
        ClassificationMethod.SHEET_NUMBER_REGEX,
        title_agrees=True,
        from_index=False,
        subtype_matched=False,
    )
    assert no_subtype == pytest.approx(0.85)

    from_index = classification_confidence(
        ClassificationMethod.TITLE_BLOCK_KEYWORDS,
        title_agrees=True,
        from_index=True,
        subtype_matched=True,
    )
    assert from_index == pytest.approx(0.63)

    assert classification_confidence(
        ClassificationMethod.HUMAN, title_agrees=False, from_index=True, subtype_matched=False
    ) == 1.0


def test_confidence_stays_inside_the_check_constraint() -> None:
    """``ck_sheet_class_conf`` is 0..1; the arithmetic must not leave it."""
    for method in ClassificationMethod:
        for agrees in (True, False):
            for indexed in (True, False):
                for matched in (True, False):
                    c = classification_confidence(
                        method,
                        title_agrees=agrees,
                        from_index=indexed,
                        subtype_matched=matched,
                    )
                    assert 0.0 <= c <= 1.0
