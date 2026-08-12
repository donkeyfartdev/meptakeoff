"""Line identity, and what a factored quantity is allowed to look like.

Three schema gaps from ``docs/output-schema.md`` §6, each of which made the
estimator-facing output either wrong or dishonest:

1. ``aggregation_key`` carried no material, so ``1/2" copper 90`` and
   ``1/2" PVC 90`` merged into one line and one total, silently.
2. A quantity produced by a factor could not be stored at all, and could not
   be labelled as factored if it had been.
3. ``SystemType`` could not tell hot domestic water from cold, which is the
   only reason insulation LF has to be factored rather than measured.

The constraint tests matter more than the happy paths: the point of
``ck_evidence_exactly_one`` and ``ck_line_requires_evidence`` is that evidence
cannot be absent, and widening the allowed shapes must not have weakened that.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, StatementError

from conduit.db.models import (
    Base,
    Derivation,
    Discipline,
    Document,
    EvidenceKind,
    ItemStatus,
    PipelineRun,
    Project,
    RunStatus,
    SystemType,
    TakeoffLine,
    TakeoffLineEvidence,
    UnitOfMeasure,
)
from conduit.db.session import create_engine_from_env, session_factory
from conduit.materials import (
    DOC_SCOPE,
    Size,
    aggregation_key,
    item_key,
    render_item_name,
    resolve_item_type,
    resolve_material,
    sheet_scope,
)
from conduit.schemas import DERIVATION_LABEL, EvidenceRef, TakeoffLineRead, derivation_label

# ---------------------------------------------------------------------------
# Gap 1 — material is part of the identity
# ---------------------------------------------------------------------------


def _elbow(material_alias: str) -> tuple:
    return (
        resolve_item_type("90"),
        resolve_material(material_alias),
        Size.nominal(0.5),
    )


def test_same_size_same_type_different_material_are_different_keys() -> None:
    """The defect this closes: one key, one total, no visible symptom."""
    copper_type, copper, half = _elbow("copper")
    pvc_type, pvc, _ = _elbow("PVC")

    copper_key = aggregation_key(
        copper_type, discipline=Discipline.PLUMBING, material=copper, size=half
    )
    pvc_key = aggregation_key(pvc_type, discipline=Discipline.PLUMBING, material=pvc, size=half)

    assert copper_key != pvc_key
    assert copper_key == "P|ELBOW_90|COPPER_WROT|0.5IN|doc"
    assert pvc_key == "P|ELBOW_90|PVC_SCH40|0.5IN|doc"

    # Same item type, same size, same discipline, same scope: material is the
    # *only* thing separating them, which is exactly the collision case.
    assert copper_type is pvc_type
    assert render_item_name(copper_type, material=copper, size=half) == '1/2" copper 90'
    assert render_item_name(pvc_type, material=pvc, size=half) == '1/2" PVC 90'


def test_dropping_material_from_the_key_is_what_made_them_collide() -> None:
    """Regression guard, stated as the old behaviour rather than the new one."""
    copper_type, copper, half = _elbow("copper")
    _, pvc, _ = _elbow("PVC")

    def material_blind(material) -> str:
        return "|".join((Discipline.PLUMBING.value, copper_type.code, half.key, DOC_SCOPE))

    assert material_blind(copper) == material_blind(pvc)  # the bug
    assert aggregation_key(
        copper_type, discipline=Discipline.PLUMBING, material=copper, size=half
    ) != aggregation_key(copper_type, discipline=Discipline.PLUMBING, material=pvc, size=half)


def test_key_is_always_five_fields_wide() -> None:
    """A materialless item takes ``-`` so a missing part cannot shift a field."""
    hanger = resolve_item_type("hanger")
    key = aggregation_key(hanger, discipline=Discipline.PLUMBING)
    assert key.split("|") == ["P", "HANGER", "-", "-", "doc"]
    assert len(key.split("|")) == 5


def test_aggregation_key_is_item_key_plus_scope() -> None:
    item_type, copper, half = _elbow("copper")
    base = item_key(item_type, discipline=Discipline.PLUMBING, material=copper, size=half)
    assert aggregation_key(
        item_type, discipline=Discipline.PLUMBING, material=copper, size=half
    ) == f"{base}|doc"
    assert aggregation_key(
        item_type,
        discipline=Discipline.PLUMBING,
        material=copper,
        size=half,
        scope=sheet_scope("P-101"),
    ) == f"{base}|sheet:P-101"


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUIT_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path.as_posix()}/t.db")
    eng = create_engine_from_env()
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def run_ids(engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    Session = session_factory(engine)
    with Session() as s:
        project = Project(name="Cedar Ridge MOB")
        s.add(project)
        s.flush()
        doc = Document(
            project_id=project.id,
            name="Permit Set Rev 2",
            original_filename="permit-rev2.pdf",
            object_key=f"projects/{project.id}/documents/original.pdf",
            sha256="a" * 64,
            byte_size=1024,
            page_count=2,
        )
        s.add(doc)
        s.flush()
        run = PipelineRun(
            document_id=doc.id,
            status=RunStatus.COMPLETED,
            is_current=True,
            code_version="test",
            render_dpi=200,
            page_count=2,
        )
        s.add(run)
        s.flush()
        s.commit()
        return run.id, project.id, doc.id


def _line(run_ids, key: str, **kw) -> TakeoffLine:
    run_id, project_id, document_id = run_ids
    kw.setdefault("quantity", Decimal("10"))
    kw.setdefault("uom", UnitOfMeasure.EACH)
    kw.setdefault("item_class", "ELBOW_90")
    kw.setdefault("discipline", Discipline.PLUMBING)
    kw.setdefault("evidence_count", 1)
    kw.setdefault("status", ItemStatus.MANUALLY_ADDED)
    if kw["status"] is ItemStatus.MANUALLY_ADDED:
        kw.setdefault("derivation", Derivation.MANUAL)
        kw["evidence_count"] = kw.get("evidence_count", 0)
    return TakeoffLine(
        pipeline_run_id=run_id,
        project_id=project_id,
        document_id=document_id,
        aggregation_key=key,
        **kw,
    )


# ---------------------------------------------------------------------------
# Gap 1, in the database
# ---------------------------------------------------------------------------


def test_copper_and_pvc_lines_coexist_and_never_merge(engine, run_ids) -> None:
    """``uq_takeoff_line_current`` is per key: distinct keys, distinct totals."""
    Session = session_factory(engine)
    with Session() as s:
        s.add(
            _line(
                run_ids,
                "P|ELBOW_90|COPPER_WROT|0.5IN|doc",
                material_code="COPPER_WROT",
                size_label='1/2"',
                quantity=Decimal("24"),
                evidence_count=0,
            )
        )
        s.add(
            _line(
                run_ids,
                "P|ELBOW_90|PVC_SCH40|0.5IN|doc",
                material_code="PVC_SCH40",
                size_label='1/2"',
                quantity=Decimal("9"),
                evidence_count=0,
            )
        )
        s.commit()

        rows = s.execute(
            select(TakeoffLine.material_code, func.sum(TakeoffLine.quantity))
            .where(TakeoffLine.item_class == "ELBOW_90", TakeoffLine.is_current)
            .group_by(TakeoffLine.material_code)
            .order_by(TakeoffLine.material_code)
        ).all()

    assert rows == [("COPPER_WROT", Decimal("24")), ("PVC_SCH40", Decimal("9"))]


def test_the_same_key_twice_in_one_run_is_still_refused(engine, run_ids) -> None:
    """The merge protection is the key's uniqueness; material only widens it."""
    Session = session_factory(engine)
    with Session() as s:
        s.add(_line(run_ids, "P|ELBOW_90|PVC_SCH40|0.5IN|doc", evidence_count=0))
        s.commit()
        s.add(_line(run_ids, "P|ELBOW_90|PVC_SCH40|0.5IN|doc", evidence_count=0))
        with pytest.raises(IntegrityError):
            s.commit()


# ---------------------------------------------------------------------------
# Gap 2 — a factored quantity is storable, and is labelled factored
# ---------------------------------------------------------------------------


def _factored_pair(run_ids) -> tuple[TakeoffLine, TakeoffLine, TakeoffLineEvidence]:
    """Hangers factored off pipe LF — the canonical case §6 called unstorable.

    The source line is a manual entry here so the fixture stays small; a source
    line whose own evidence resolves to a sheet, page and bbox is exercised by
    the design package's ``verify_models.py`` provenance round-trip.
    """
    pipe = _line(
        run_ids,
        "P|PIPE|COPPER_TYPE_L|0.75IN|doc",
        item_class="PIPE",
        material_code="COPPER_TYPE_L",
        size_label='3/4"',
        system_type=SystemType.PIPE_DOMESTIC_HOT,
        uom=UnitOfMeasure.LINEAR_FEET,
        quantity=Decimal("210.0000"),
        status=ItemStatus.MANUALLY_ADDED,
        derivation=Derivation.MANUAL,
        evidence_count=0,
    )
    hangers = _line(
        run_ids,
        "P|HANGER|-|-|doc",
        item_class="HANGER",
        quantity=Decimal("32"),
        auto_quantity=Decimal("32"),
        status=ItemStatus.AUTO,
        derivation=Derivation.FACTORED,
        factor_rule_id="hanger_spacing",
        factor_rule_version="v1",
        factor_value=Decimal("0.142857"),
        factor_basis={"spacing_ft": 7.0, "ends_per_run": 2, "source": "project default"},
        evidence_count=1,
    )
    evidence = TakeoffLineEvidence(
        evidence_kind=EvidenceKind.DERIVED_FROM_LINE,
        contribution_qty=Decimal("32"),
        note="1 hanger / 7 ft over 210 LF, + 2 ends per run",
        extractor_version="hanger_spacing@v1",
    )
    return pipe, hangers, evidence


def test_a_factored_line_is_representable(engine, run_ids) -> None:
    Session = session_factory(engine)
    with Session() as s:
        pipe, hangers, evidence = _factored_pair(run_ids)
        s.add_all([pipe, hangers])
        s.flush()
        evidence.takeoff_line_id = hangers.id
        evidence.source_takeoff_line_id = pipe.id
        s.add(evidence)
        s.commit()

        stored = s.get(TakeoffLine, hangers.id)
        assert stored.derivation is Derivation.FACTORED
        assert len(stored.evidence) == 1
        basis = stored.evidence[0]
        assert basis.evidence_kind is EvidenceKind.DERIVED_FROM_LINE
        assert basis.source_takeoff_line_id == pipe.id
        # The chain resolves: the cited line is a real row, not a note.
        assert s.get(TakeoffLine, basis.source_takeoff_line_id).item_class == "PIPE"
        assert basis.contribution_qty == stored.auto_quantity


def test_a_factored_line_must_carry_its_factor(engine, run_ids) -> None:
    """``ck_line_factored_carries_factor``: a number nobody can re-derive is
    the number this schema refuses to hold."""
    Session = session_factory(engine)
    with Session() as s:
        line = _line(
            run_ids,
            "P|HANGER|-|-|doc",
            item_class="HANGER",
            status=ItemStatus.AUTO,
            derivation=Derivation.FACTORED,
            evidence_count=1,
        )
        s.add(line)
        with pytest.raises(IntegrityError):
            s.commit()


def test_an_evidence_free_line_is_still_rejected(engine, run_ids) -> None:
    """The constraint was widened, not weakened: zero evidence is still zero."""
    Session = session_factory(engine)
    with Session() as s:
        s.add(
            _line(
                run_ids,
                "P|ELBOW_90|PVC_SCH40|0.5IN|doc",
                status=ItemStatus.AUTO,
                derivation=Derivation.COUNTED,
                evidence_count=0,
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_evidence_with_no_source_at_all_is_still_rejected(engine, run_ids) -> None:
    Session = session_factory(engine)
    with Session() as s:
        line = _line(run_ids, "P|ELBOW_90|PVC_SCH40|0.5IN|doc", evidence_count=0)
        s.add(line)
        s.flush()
        s.add(
            TakeoffLineEvidence(
                takeoff_line_id=line.id,
                evidence_kind=EvidenceKind.DERIVED_FROM_LINE,
                contribution_qty=Decimal("1"),
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_evidence_with_two_sources_is_still_rejected(engine, run_ids) -> None:
    Session = session_factory(engine)
    with Session() as s:
        line = _line(run_ids, "P|ELBOW_90|PVC_SCH40|0.5IN|doc", evidence_count=0)
        other = _line(run_ids, "P|PIPE|PVC_SCH40|0.5IN|doc", item_class="PIPE", evidence_count=0)
        s.add_all([line, other])
        s.flush()
        s.add(
            TakeoffLineEvidence(
                takeoff_line_id=line.id,
                evidence_kind=EvidenceKind.DERIVED_FROM_LINE,
                source_takeoff_line_id=other.id,
                detection_id=uuid.uuid4(),
                contribution_qty=Decimal("1"),
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_line_cannot_be_its_own_basis(engine, run_ids) -> None:
    Session = session_factory(engine)
    with Session() as s:
        line = _line(run_ids, "P|HANGER|-|-|doc", item_class="HANGER", evidence_count=0)
        s.add(line)
        s.flush()
        s.add(
            TakeoffLineEvidence(
                takeoff_line_id=line.id,
                evidence_kind=EvidenceKind.DERIVED_FROM_LINE,
                source_takeoff_line_id=line.id,
                contribution_qty=Decimal("1"),
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_factored_is_surfaced_on_the_line_the_estimator_reads() -> None:
    read = TakeoffLineRead(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        aggregation_key="P|HANGER|-|-|doc",
        discipline=Discipline.PLUMBING,
        item_class="HANGER",
        quantity=Decimal("32"),
        uom=UnitOfMeasure.EACH,
        derivation=Derivation.FACTORED,
        factor_rule_id="hanger_spacing",
        factor_rule_version="v1",
        factor_value=Decimal("0.142857"),
        status=ItemStatus.MANUALLY_ADDED,
    )
    assert read.derivation_note == "factored: hanger_spacing v1"
    assert derivation_label(Derivation.COUNTED) == "counted"
    assert derivation_label(Derivation.MEASURED) == "measured"
    assert set(DERIVATION_LABEL) == set(Derivation)


def test_a_factored_read_model_must_carry_its_factor() -> None:
    with pytest.raises(ValueError, match="factor_rule_id"):
        TakeoffLineRead(
            id=uuid.uuid4(),
            pipeline_run_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            aggregation_key="P|HANGER|-|-|doc",
            discipline=Discipline.PLUMBING,
            item_class="HANGER",
            quantity=Decimal("32"),
            uom=UnitOfMeasure.EACH,
            derivation=Derivation.FACTORED,
            status=ItemStatus.MANUALLY_ADDED,
        )


def test_evidence_ref_binds_the_new_kind_to_the_new_field() -> None:
    source = uuid.uuid4()
    ref = EvidenceRef(
        id=uuid.uuid4(),
        evidence_kind=EvidenceKind.DERIVED_FROM_LINE,
        source_takeoff_line_id=source,
        contribution_qty=Decimal("32"),
    )
    assert ref.source_takeoff_line_id == source

    with pytest.raises(ValueError, match="source_takeoff_line_id"):
        EvidenceRef(
            id=uuid.uuid4(),
            evidence_kind=EvidenceKind.DERIVED_FROM_LINE,
            detection_id=uuid.uuid4(),
        )
    with pytest.raises(ValueError, match="exactly one"):
        EvidenceRef(id=uuid.uuid4(), evidence_kind=EvidenceKind.DERIVED_FROM_LINE)


# ---------------------------------------------------------------------------
# Gap 3 — hot and cold domestic water are distinguishable
# ---------------------------------------------------------------------------


def test_domestic_water_services_are_distinct_members() -> None:
    services = {
        SystemType.PIPE_DOMESTIC_COLD,
        SystemType.PIPE_DOMESTIC_HOT,
        SystemType.PIPE_DOMESTIC_RECIRC,
    }
    assert len({s.value for s in services}) == 3
    # The old value survives as "service not determined" — never a synonym for
    # cold, because guessing cold would put insulation on the wrong runs.
    assert SystemType.PIPE_DOMESTIC_WATER not in services
    assert SystemType("pipe_domestic_hot") is SystemType.PIPE_DOMESTIC_HOT


def test_hot_and_cold_lines_are_separable_by_query(engine, run_ids) -> None:
    """This is what moves insulation from factored to measured: the hot runs
    can be selected. No claim is made here about the insulation rule itself."""
    Session = session_factory(engine)
    with Session() as s:
        for key, service, qty in (
            ("P|PIPE|COPPER_TYPE_L|0.75IN|doc:hot", SystemType.PIPE_DOMESTIC_HOT, "210"),
            ("P|PIPE|COPPER_TYPE_L|0.75IN|doc:cold", SystemType.PIPE_DOMESTIC_COLD, "455"),
            ("P|PIPE|COPPER_TYPE_L|0.75IN|doc:unk", SystemType.PIPE_DOMESTIC_WATER, "18"),
        ):
            s.add(
                _line(
                    run_ids,
                    key,
                    item_class="PIPE",
                    system_type=service,
                    uom=UnitOfMeasure.LINEAR_FEET,
                    quantity=Decimal(qty),
                    evidence_count=0,
                )
            )
        s.commit()

        hot = s.execute(
            select(func.sum(TakeoffLine.quantity)).where(
                TakeoffLine.system_type == SystemType.PIPE_DOMESTIC_HOT
            )
        ).scalar_one()
        undetermined = s.execute(
            select(func.sum(TakeoffLine.quantity)).where(
                TakeoffLine.system_type == SystemType.PIPE_DOMESTIC_WATER
            )
        ).scalar_one()

    assert hot == Decimal("210")
    assert undetermined == Decimal("18")


def test_an_unknown_system_type_string_is_refused(engine, run_ids) -> None:
    """``validate_strings=True``: the enum widened, it did not become free text."""
    Session = session_factory(engine)
    with Session() as s:
        s.add(
            _line(
                run_ids,
                "P|PIPE|COPPER_TYPE_L|0.75IN|doc",
                item_class="PIPE",
                system_type="pipe_domestic_tepid",
                evidence_count=0,
            )
        )
        with pytest.raises((StatementError, LookupError, ValueError)):
            s.commit()
