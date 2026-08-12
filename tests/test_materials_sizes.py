"""Canonical-size tests.

The load-bearing one is ``test_key_is_exactly_stage_a_normalisation``: the
whole point of ``Size.key`` is that it equals what stage A already writes into
``TextSpan.normalized_text``, so a size tag read off a drawing joins to the
vocabulary by equality. If the stage A normaliser ever changes, this file is
the alarm.
"""

from __future__ import annotations

import pytest

from conduit.ingest.textlines import normalize_text
from conduit.materials.sizes import Size, SizeKind, format_inches, parse_size


@pytest.mark.parametrize(
    ("inches", "expected"),
    [
        (0.125, '1/8"'),
        (0.25, '1/4"'),
        (0.5, '1/2"'),
        (0.75, '3/4"'),
        (1.0, '1"'),
        (1.25, '1-1/4"'),
        (1.5, '1-1/2"'),
        (2.0, '2"'),
        (2.5, '2-1/2"'),
        (4.0, '4"'),
        (6.0, '6"'),
    ],
)
def test_format_inches_trade_shorthand(inches: float, expected: str) -> None:
    assert format_inches(inches) == expected


@pytest.mark.parametrize("bad", [0.1, 1.0 / 3.0, 0.0, -2.0])
def test_format_inches_refuses_non_trade_values(bad: float) -> None:
    with pytest.raises(ValueError):
        format_inches(bad)


ALL_SIZES = [
    Size.none(),
    Size.nominal(0.5),
    Size.nominal(1.25),
    Size.nominal(4),
    Size.reducer(0.75, 0.5),
    Size.reducer(2, 1.5),
    Size.rectangular(12, 8),
    Size.rectangular(24, 24),
    Size.wire_gauge("#12 AWG"),
    Size.wire_gauge("4/0"),
    Size.wire_gauge("250 kcmil"),
    Size.label("75 gal"),
    Size.label("1.28 GPF"),
]


@pytest.mark.parametrize("size", ALL_SIZES, ids=lambda s: s.key or "none")
def test_key_is_exactly_stage_a_normalisation(size: Size) -> None:
    assert size.key == normalize_text(size.display)


@pytest.mark.parametrize("size", ALL_SIZES, ids=lambda s: s.key or "none")
def test_key_is_idempotent(size: Size) -> None:
    """Normalising an already-normalised key must not move it again."""
    assert normalize_text(size.key) == size.key


@pytest.mark.parametrize(
    ("expected_key", "spellings"),
    [
        ("0.75IN", ['3/4"', "3/4", "3/4 in", "3/4 IN", "0.75IN", '.75"', "0.75"]),
        ("1.25IN", ['1-1/4"', "1 1/4", "1.25", '1.25"', "1-1/4 IN"]),
        ("2IN", ['2"', "2", "2 in", "2.0"]),
        ("250 KCMIL", ["250 kcmil", "250 KCMIL", "250 MCM", "250 mcm"]),
        ("#12 AWG", ["#12 AWG", "12 awg", "#12", "  #12   awg "]),
    ],
)
def test_two_spellings_collapse_to_one_key(expected_key: str, spellings: list[str]) -> None:
    keys = {parse_size(s).key for s in spellings}
    assert keys == {expected_key}


def test_reducer_always_writes_the_larger_end_first() -> None:
    assert Size.reducer(0.5, 0.75) == Size.reducer(0.75, 0.5)
    assert Size.reducer(0.5, 0.75).display == '3/4" x 1/2"'


def test_reducer_parses_from_text() -> None:
    size = parse_size('3/4" x 1/2"')
    assert size.kind is SizeKind.REDUCER
    assert size.display == '3/4" x 1/2"'
    assert size.primary_in == 0.75 and size.secondary_in == 0.5


def test_the_one_ambiguity_is_resolved_by_the_caller_not_guessed() -> None:
    """``2 x 1`` is a reducer to a plumber and a duct to a sheet-metal hand."""
    assert parse_size("2 x 1").kind is SizeKind.REDUCER
    assert parse_size("2 x 1", prefer=SizeKind.RECTANGULAR).kind is SizeKind.RECTANGULAR
    # An inch-marked pair is never rectangular, whatever the caller prefers.
    assert parse_size('2" x 1"', prefer=SizeKind.RECTANGULAR).kind is SizeKind.REDUCER


def test_rectangular_duct_keeps_the_tag_form() -> None:
    size = Size.rectangular(12, 8)
    assert size.display == "12x8"
    assert size.key == "12X8"
    assert parse_size("12 X 8", prefer=SizeKind.RECTANGULAR) == size


@pytest.mark.parametrize("junk", ["", "   ", "as noted", "N/A", "12x8x6", None])
def test_unparseable_sizes_return_none_rather_than_a_guess(junk) -> None:
    assert parse_size(junk) is None


def test_no_size_is_falsy_and_renders_as_nothing() -> None:
    none = Size.none()
    assert not none
    assert none.display == ""
    assert none.key == ""
    assert bool(Size.nominal(0.5)) is True
