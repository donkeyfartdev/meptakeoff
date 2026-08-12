"""Conduit Takeoff — SQLAlchemy 2.0 ORM models.

Target: PostgreSQL 16. Portable enough to run against SQLite for tests
(``metadata.create_all``) — JSONB/ARRAY are declared as dialect variants and
enums are stored as VARCHAR + CHECK rather than native PG enums, so adding a
class name later is a data migration rather than an ``ALTER TYPE``.

=============================================================================
COORDINATE SPACES  (read this before touching any bbox)
=============================================================================
Two spaces exist. Every geometry-bearing row names the one it is in via a
``coordinate_space`` column. There is no implicit space anywhere.

1. ``pdf_points`` — the PDF user space of the page, 72 units per inch, origin
   at the MediaBox lower-left, y increasing UP. This is what PyMuPDF's text and
   drawing extraction returns. It is DPI-independent and therefore the
   canonical space for anything derived from vector content (``TextSpan``,
   ``ScheduleTable``, ``ScheduleRow``, ``Measurement``).

2. ``raster_px`` — pixels of the rendered page image, origin at the TOP-LEFT,
   y increasing DOWN, at exactly ``Sheet.render_dpi``. This is what YOLO sees
   and therefore the space ``Detection`` boxes are stored in. Detections also
   denormalise ``render_dpi`` onto the row so the box stays interpretable even
   if the sheet is later re-rendered at another DPI by another run.

Conversion (unrotated page, ``Sheet.rotation_deg == 0``)::

    s   = render_dpi / 72.0
    x_pt = media_box_x0 + x_px / s
    y_pt = media_box_y1 - y_px / s          # y flip
    x_px = (x_pt - media_box_x0) * s
    y_px = (media_box_y1 - y_pt) * s

For ``rotation_deg in (90, 180, 270)`` apply the page rotation about the
MediaBox centre first; ``Sheet`` stores ``rotation_deg`` and all four MediaBox
edges precisely so the transform is always recoverable. ``Sheet.width_px`` /
``height_px`` are the dimensions of the rendered (post-rotation) image.

=============================================================================
IMMUTABILITY
=============================================================================
Evidence tables (``TextSpan``, ``ScheduleTable``, ``ScheduleRow``,
``Detection``, ``Measurement``) and the log tables (``ReviewAction``,
``AuditEvent``) are APPEND-ONLY. They have ``created_at`` and no
``updated_at``/``deleted_at`` by design. Corrections are new ``ReviewAction``
rows; re-processing is a new ``PipelineRun``. The application layer must not
issue UPDATE or DELETE against them (enforce with a DB role that lacks the
grants, plus a ``before_update`` event hook in tests).

``TakeoffLine`` is derived and therefore versioned rather than mutated: the
aggregator writes ``revision + 1`` and flips ``is_current``, so a
``takeoff_line_id`` referenced by an old export still resolves.

Soft delete (``deleted_at``) exists only on ``Project`` and ``Document``, the
two user-facing containers.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# JSONB on Postgres, plain JSON elsewhere (SQLite tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never use datetime.utcnow() in this codebase."""
    return datetime.now(timezone.utc)


def _enum(py_enum: type[enum.Enum], name: str) -> SAEnum:
    """VARCHAR + CHECK enum. Portable, and cheap to extend."""
    return SAEnum(
        py_enum,
        name=name,
        native_enum=False,
        length=48,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Discipline(str, enum.Enum):
    ELECTRICAL = "E"
    PLUMBING = "P"
    MECHANICAL = "M"
    ARCHITECTURAL = "A"
    STRUCTURAL = "S"
    FIRE_PROTECTION = "FP"
    CIVIL = "C"
    GENERAL = "G"
    UNKNOWN = "UNKNOWN"


class SheetSubtype(str, enum.Enum):
    """Discipline sub-classification. Drives which detectors/rules run."""

    E_LIGHTING = "E_LIGHTING"
    E_POWER = "E_POWER"
    E_FIRE_ALARM = "E_FIRE_ALARM"
    E_LOW_VOLTAGE = "E_LOW_VOLTAGE"
    E_ONE_LINE = "E_ONE_LINE"
    E_SCHEDULE = "E_SCHEDULE"
    E_SITE = "E_SITE"
    P_SANITARY = "P_SANITARY"
    P_DOMESTIC_WATER = "P_DOMESTIC_WATER"
    P_GAS = "P_GAS"
    P_STORM = "P_STORM"
    P_RISER = "P_RISER"
    P_SCHEDULE = "P_SCHEDULE"
    M_DUCT = "M_DUCT"
    M_PIPING = "M_PIPING"
    M_EQUIPMENT = "M_EQUIPMENT"
    M_CONTROLS = "M_CONTROLS"
    M_SCHEDULE = "M_SCHEDULE"
    FP_SPRINKLER = "FP_SPRINKLER"
    LEGEND = "LEGEND"
    DETAIL = "DETAIL"
    COVER = "COVER"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ClassificationMethod(str, enum.Enum):
    SHEET_NUMBER_REGEX = "sheet_number_regex"
    TITLE_BLOCK_KEYWORDS = "title_block_keywords"
    THUMBNAIL_CLASSIFIER = "thumbnail_classifier"
    HUMAN = "human"
    DEFAULT_FALLBACK = "default_fallback"


class CoordinateSpace(str, enum.Enum):
    PDF_POINTS = "pdf_points"
    RASTER_PX = "raster_px"


class ScaleSource(str, enum.Enum):
    SHEET_TEXT = "sheet_text"          # parsed from a scale string on the sheet
    TITLE_BLOCK = "title_block"
    GRAPHIC_BAR = "graphic_bar"        # measured off a graphic scale bar
    CALIBRATION = "calibration"        # derived from a known dimension string
    HUMAN = "human"                    # an estimator set it
    PROJECT_DEFAULT = "project_default"
    UNKNOWN = "unknown"


class TextSource(str, enum.Enum):
    VECTOR = "vector"   # from the PDF text layer — trusted
    OCR = "ocr"         # from PaddleOCR/Tesseract — lower trust
    HUMAN = "human"


class SpanRole(str, enum.Enum):
    UNKNOWN = "unknown"
    TITLE_BLOCK = "title_block"
    SHEET_NUMBER = "sheet_number"
    SCALE_STRING = "scale_string"
    DEVICE_TAG = "device_tag"
    EQUIPMENT_TAG = "equipment_tag"
    SIZE_LABEL = "size_label"
    DIMENSION = "dimension"
    SCHEDULE_CELL = "schedule_cell"
    ROOM_NAME = "room_name"
    NOTE = "note"
    LEGEND_ENTRY = "legend_entry"


class ScheduleKind(str, enum.Enum):
    LIGHTING_FIXTURE = "lighting_fixture"
    PANEL = "panel"
    ELECTRICAL_EQUIPMENT = "electrical_equipment"
    PLUMBING_FIXTURE = "plumbing_fixture"
    MECHANICAL_EQUIPMENT = "mechanical_equipment"
    DIFFUSER = "diffuser"
    VALVE = "valve"
    DOOR = "door"
    OTHER = "other"
    UNKNOWN = "unknown"


class SystemType(str, enum.Enum):
    """What a linear run physically is."""

    CONDUIT = "conduit"
    CABLE_TRAY = "cable_tray"
    WIRE = "wire"
    #: Domestic water whose service (hot/cold/recirc) was not determined. Kept
    #: as the honest "we could not tell" value — never a synonym for cold.
    PIPE_DOMESTIC_WATER = "pipe_domestic_water"
    PIPE_DOMESTIC_COLD = "pipe_domestic_cold"
    #: Hot and recirculation are the insulated services. Splitting them out is
    #: what lets insulation LF be *measured* off the hot runs with real
    #: provenance instead of factored off a total that mixes hot with cold.
    PIPE_DOMESTIC_HOT = "pipe_domestic_hot"
    PIPE_DOMESTIC_RECIRC = "pipe_domestic_recirc"
    PIPE_SANITARY = "pipe_sanitary"
    PIPE_STORM = "pipe_storm"
    PIPE_GAS = "pipe_gas"
    PIPE_HYDRONIC = "pipe_hydronic"
    PIPE_SPRINKLER = "pipe_sprinkler"
    DUCT_SUPPLY = "duct_supply"
    DUCT_RETURN = "duct_return"
    DUCT_EXHAUST = "duct_exhaust"
    UNKNOWN = "unknown"


class RiseSource(str, enum.Enum):
    """How a vertical component was justified. NONE means no rise claimed."""

    NONE = "none"
    CEILING_HEIGHT_NOTE = "ceiling_height_note"
    RISER_DIAGRAM = "riser_diagram"
    RISE_SYMBOL = "rise_symbol"            # up/down arrow or riser tick on plan
    FLOOR_TO_FLOOR = "floor_to_floor"
    PROJECT_DEFAULT = "project_default"
    HUMAN = "human"


class ItemStatus(str, enum.Enum):
    """Lifecycle of any quantity-bearing row."""

    AUTO = "auto"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    MANUALLY_ADDED = "manually_added"


class UnitOfMeasure(str, enum.Enum):
    EACH = "EA"
    LINEAR_FEET = "LF"
    SQUARE_FEET = "SF"
    CUBIC_FEET = "CF"
    POUNDS = "LB"
    HOURS = "HR"
    LOT = "LOT"


class EvidenceKind(str, enum.Enum):
    DETECTION = "detection"
    TEXT_SPAN = "text_span"
    SCHEDULE_ROW = "schedule_row"
    MEASUREMENT = "measurement"
    MANUAL = "manual"
    #: The evidence is another takeoff line: hangers factored off pipe LF,
    #: insulation factored off hot-water LF. The cited line carries its own
    #: evidence, so the chain still terminates at a sheet, page and bbox.
    DERIVED_FROM_LINE = "derived_from_line"


class Derivation(str, enum.Enum):
    """How a line's quantity came to be — the thing the estimator must be told.

    Distinct from ``ItemStatus``, which is a review lifecycle. A number that
    was factored off another number is not the same claim as one that was
    counted off a drawing, and presenting the two identically is the quiet
    kind of wrongness this schema exists to prevent.
    """

    COUNTED = "counted"                     # discrete evidence, one per unit
    MEASURED = "measured"                   # polyline length against a scale
    DERIVED_GEOMETRIC = "derived_geometric"  # inferred from geometry (a vertex)
    FACTORED = "factored"                   # a rule applied to another quantity
    MANUAL = "manual"                       # a human typed it


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageName(str, enum.Enum):
    RASTERISE = "rasterise"
    CLASSIFY = "classify"
    TEXT_INDEX = "text_index"
    OCR = "ocr"
    DETECT = "detect"
    MEASURE = "measure"
    AGGREGATE = "aggregate"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    INHERITED = "inherited"   # reused from parent run, not recomputed


class ReviewActionType(str, enum.Enum):
    CONFIRM = "confirm"
    REJECT = "reject"
    EDIT_QUANTITY = "edit_quantity"
    EDIT_CLASS = "edit_class"
    EDIT_SIZE = "edit_size"
    EDIT_GEOMETRY = "edit_geometry"
    ADD_MANUAL = "add_manual"
    SET_SCALE = "set_scale"
    RECLASSIFY_SHEET = "reclassify_sheet"
    SET_RISE = "set_rise"
    NOTE = "note"


class ActorType(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"


class ExportFormat(str, enum.Enum):
    XLSX = "xlsx"
    CSV = "csv"
    JSON = "json"


class ExportStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ProjectStatus(str, enum.Enum):
    ACTIVE = "active"
    BIDDING = "bidding"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Base + mixins
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    type_annotation_map = {
        dict: JSONType,
        list: JSONType,
    }


def pk_column() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class MutableTimestampMixin(TimestampMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProvenanceMixin:
    """Every evidence row answers: which run, which sheet, which page, where."""

    @property
    def provenance_key(self) -> str:  # pragma: no cover - convenience for logs
        return f"{self.sheet_id}:{self.page_number}"


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


class Project(MutableTimestampMixin, Base):
    __tablename__ = "project"

    id: Mapped[uuid.UUID] = pk_column()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_name: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    bid_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ProjectStatus] = mapped_column(
        _enum(ProjectStatus, "project_status"), nullable=False, default=ProjectStatus.ACTIVE
    )
    #: Fallback assumptions used when a sheet gives us nothing, e.g.
    #: {"default_ceiling_height_ft": 10.0, "default_rise_ft": 1.5}
    defaults: Mapped[dict | None] = mapped_column(JSONType)
    created_by: Mapped[str | None] = mapped_column(String(128))

    documents: Mapped[list[Document]] = relationship(back_populates="project")

    __table_args__ = (Index("ix_project_status_created", "status", "created_at"),)


class Document(MutableTimestampMixin, Base):
    """A plan set: one uploaded PDF. 'PlanSet' in prose, ``document`` in SQL."""

    __tablename__ = "document"

    id: Mapped[uuid.UUID] = pk_column()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Object-storage key of the untouched upload. Never rewritten.
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Issue/revision label from the plan set itself, e.g. "Addendum 2".
    revision_label: Mapped[str | None] = mapped_column(String(128))
    uploaded_by: Mapped[str | None] = mapped_column(String(128))

    #: The run whose takeoff is served by default. Switched in one audited
    #: transaction; prior runs remain fully queryable.
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_run.id", use_alter=True, name="fk_document_current_run",
                   ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="documents")
    sheets: Mapped[list[Sheet]] = relationship(back_populates="document")
    runs: Mapped[list[PipelineRun]] = relationship(
        back_populates="document", foreign_keys="PipelineRun.document_id"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="uq_document_project_sha256"),
        Index("ix_document_project", "project_id"),
        CheckConstraint("page_count > 0", name="ck_document_pages_positive"),
    )


class Sheet(TimestampMixin, Base):
    """One page of a plan set, plus its classification and page geometry.

    Classification columns are updated in place because a sheet is a container,
    not evidence — but every change goes through a ``ReviewAction`` +
    ``AuditEvent`` pair, so the history is still recoverable.
    """

    __tablename__ = "sheet"

    id: Mapped[uuid.UUID] = pk_column()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    sheet_number: Mapped[str | None] = mapped_column(String(64))       # "E-201"
    sheet_title: Mapped[str | None] = mapped_column(String(512))

    discipline: Mapped[Discipline] = mapped_column(
        _enum(Discipline, "discipline"), nullable=False, default=Discipline.UNKNOWN
    )
    subtype: Mapped[SheetSubtype] = mapped_column(
        _enum(SheetSubtype, "sheet_subtype"), nullable=False, default=SheetSubtype.UNKNOWN
    )
    classification_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    classification_method: Mapped[ClassificationMethod] = mapped_column(
        _enum(ClassificationMethod, "classification_method"),
        nullable=False,
        default=ClassificationMethod.DEFAULT_FALLBACK,
    )
    classification_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="SET NULL")
    )

    # --- page geometry: the full transform between pdf_points and raster_px ---
    media_box_x0: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    media_box_y0: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    media_box_x1: Mapped[float] = mapped_column(Float, nullable=False)
    media_box_y1: Mapped[float] = mapped_column(Float, nullable=False)
    rotation_deg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    render_dpi: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)

    # --- artifacts ---
    #: Run that owns the pixels these keys point at; a re-run at the same DPI
    #: reuses them rather than re-rendering 200 pages.
    raster_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="SET NULL")
    )
    raster_object_key: Mapped[str | None] = mapped_column(String(1024))
    thumbnail_object_key: Mapped[str | None] = mapped_column(String(1024))
    tile_base_key: Mapped[str | None] = mapped_column(String(1024))
    tile_max_zoom: Mapped[int | None] = mapped_column(Integer)
    #: Vector-path dump for the page (``paths.json.zst``), written by stage A
    #: and read by D and E. It is a column rather than an audit-payload key
    #: because provenance a reviewer has to reconstruct out of JSON is not
    #: queryable provenance: "which sheets have no vector paths" must be a
    #: WHERE clause.
    paths_object_key: Mapped[str | None] = mapped_column(String(1024))

    has_vector_text: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ocr_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    text_span_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    document: Mapped[Document] = relationship(back_populates="sheets", foreign_keys=[document_id])
    scales: Mapped[list[SheetScale]] = relationship(back_populates="sheet")

    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_sheet_document_page"),
        Index("ix_sheet_document_discipline", "document_id", "discipline"),
        Index("ix_sheet_number", "document_id", "sheet_number"),
        CheckConstraint("page_number >= 1", name="ck_sheet_page_number"),
        CheckConstraint("rotation_deg IN (0, 90, 180, 270)", name="ck_sheet_rotation"),
        CheckConstraint("render_dpi BETWEEN 36 AND 1200", name="ck_sheet_dpi"),
        CheckConstraint(
            "classification_confidence >= 0.0 AND classification_confidence <= 1.0",
            name="ck_sheet_class_conf",
        ),
    )


class SheetScale(TimestampMixin, Base):
    """Drawing scale for a sheet. Append-only: a human override is a new row.

    ``scale_ratio`` is dimensionless real-world-over-paper (1/8" = 1'-0" -> 96).
    ``feet_per_paper_point`` is the derived multiplier the measurement stage
    actually uses, stored explicitly so no consumer has to re-derive it:

        feet_per_paper_point = scale_ratio / (12.0 * 72.0)
    """

    __tablename__ = "sheet_scale"

    id: Mapped[uuid.UUID] = pk_column()
    sheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sheet.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="SET NULL")
    )
    #: Viewport/detail this scale applies to; NULL = whole sheet.
    viewport_label: Mapped[str | None] = mapped_column(String(128))
    scale_text: Mapped[str | None] = mapped_column(String(128))       # '1/8" = 1\'-0"'
    scale_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    feet_per_paper_point: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[ScaleSource] = mapped_column(
        _enum(ScaleSource, "scale_source"), nullable=False, default=ScaleSource.UNKNOWN
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: The span the scale string was read from, when source == SHEET_TEXT.
    source_text_span_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("text_span.id", ondelete="SET NULL")
    )
    review_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_action.id", ondelete="SET NULL")
    )

    sheet: Mapped[Sheet] = relationship(back_populates="scales")

    __table_args__ = (
        Index(
            "uq_sheet_scale_current",
            "sheet_id",
            "viewport_label",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current"),
        ),
        CheckConstraint("scale_ratio > 0", name="ck_scale_ratio_positive"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_scale_conf"),
    )


# ---------------------------------------------------------------------------
# Run lineage
# ---------------------------------------------------------------------------


class PipelineRun(TimestampMixin, Base):
    """One execution of some subset of stages over one document.

    Every evidence row and every takeoff line FKs to exactly one run. A run
    that does not execute a stage *inherits* the parent run's rows for that
    stage (see ``inherited_stages``), which is what makes "re-detect with new
    weights" cheap: 200 pages are not re-rasterised or re-indexed.
    """

    __tablename__ = "pipeline_run"

    id: Mapped[uuid.UUID] = pk_column()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="SET NULL")
    )
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), nullable=False, default=RunStatus.QUEUED
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: e.g. ["rasterise","classify","text_index","detect","measure","aggregate"]
    stages_executed: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: Stages served from ``parent_run_id`` instead of recomputed.
    inherited_stages: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    code_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    config_hash: Mapped[str | None] = mapped_column(String(64))
    #: {"detector":"yolov8s-mep@2026.08.1","ocr":"ppocr-v4","text":"pymupdf-1.24.9"}
    model_versions: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    render_dpi: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Snapshot of the SSE progress object, flushed every ~5s so a late or
    #: reconnecting client gets truth from the DB rather than from Redis.
    progress: Mapped[dict | None] = mapped_column(JSONType)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(
        back_populates="runs", foreign_keys=[document_id]
    )

    __table_args__ = (
        Index(
            "uq_pipeline_run_current",
            "document_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current"),
        ),
        Index("ix_run_document_created", "document_id", "created_at"),
        CheckConstraint("render_dpi BETWEEN 36 AND 1200", name="ck_run_dpi"),
    )


class PageTaskState(TimestampMixin, Base):
    """Per-page, per-stage progress. The aggregate barrier reads this table."""

    __tablename__ = "page_task_state"

    id: Mapped[uuid.UUID] = pk_column()
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    sheet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sheet.id", ondelete="CASCADE")
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[StageName] = mapped_column(_enum(StageName, "stage_name"), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus, "task_status"), nullable=False, default=TaskStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    peak_rss_mb: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "pipeline_run_id", "page_number", "stage", name="uq_page_task_run_page_stage"
        ),
        Index("ix_page_task_run_status", "pipeline_run_id", "status"),
    )


# ---------------------------------------------------------------------------
# Evidence: text
# ---------------------------------------------------------------------------


class TextSpan(TimestampMixin, ProvenanceMixin, Base):
    """A run of text from the PDF text layer (or OCR). Immutable.

    Geometry is in ``pdf_points``. ``rotation_deg`` is the text baseline angle,
    not the page rotation.
    """

    __tablename__ = "text_span"

    id: Mapped[uuid.UUID] = pk_column()
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    sheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sheet.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Canonicalised form for matching: uppercased, whitespace-collapsed,
    #: fractions normalised ('3/4"' -> '0.75IN').
    normalized_text: Mapped[str | None] = mapped_column(String(512))

    coordinate_space: Mapped[CoordinateSpace] = mapped_column(
        _enum(CoordinateSpace, "coordinate_space"),
        nullable=False,
        default=CoordinateSpace.PDF_POINTS,
    )
    bbox_x0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)
    rotation_deg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    font_name: Mapped[str | None] = mapped_column(String(128))
    font_size_pt: Mapped[float | None] = mapped_column(Float)
    source: Mapped[TextSource] = mapped_column(
        _enum(TextSource, "text_source"), nullable=False, default=TextSource.VECTOR
    )
    #: 1.0 for vector text; the OCR score for OCR'd text.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    role: Mapped[SpanRole] = mapped_column(
        _enum(SpanRole, "span_role"), nullable=False, default=SpanRole.UNKNOWN
    )
    #: Parsed payload for typed roles, e.g. {"inches": 0.75} or
    #: {"scale_ratio": 96.0} or {"tag": "F2"}.
    parsed_value: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        Index("ix_text_span_sheet", "sheet_id"),
        Index("ix_text_span_run_role", "pipeline_run_id", "role"),
        Index("ix_text_span_normalized", "normalized_text"),
        CheckConstraint("bbox_x1 >= bbox_x0 AND bbox_y1 >= bbox_y0", name="ck_text_span_bbox"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_text_span_conf"),
        # Postgres only: CREATE INDEX ... USING gin (normalized_text gin_trgm_ops)
        # is added in the Alembic migration for fuzzy tag search.
    )


class ScheduleTable(TimestampMixin, ProvenanceMixin, Base):
    """A schedule recovered from a sheet. Geometry in ``pdf_points``."""

    __tablename__ = "schedule_table"

    id: Mapped[uuid.UUID] = pk_column()
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    sheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sheet.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str | None] = mapped_column(String(512))
    kind: Mapped[ScheduleKind] = mapped_column(
        _enum(ScheduleKind, "schedule_kind"), nullable=False, default=ScheduleKind.UNKNOWN
    )
    coordinate_space: Mapped[CoordinateSpace] = mapped_column(
        _enum(CoordinateSpace, "coordinate_space"),
        nullable=False,
        default=CoordinateSpace.PDF_POINTS,
    )
    bbox_x0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)

    header_labels: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: x-positions (pdf points) of detected column boundaries.
    column_x_positions: Mapped[list | None] = mapped_column(JSONType)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")

    rows: Mapped[list[ScheduleRow]] = relationship(back_populates="table")

    __table_args__ = (
        Index("ix_schedule_table_sheet", "sheet_id"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_sched_table_conf"),
    )


class ScheduleRow(TimestampMixin, Base):
    """One row of a schedule — the highest-trust evidence we produce."""

    __tablename__ = "schedule_row"

    id: Mapped[uuid.UUID] = pk_column()
    schedule_table_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schedule_table.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    sheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sheet.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Raw cell values keyed by header label, plus a "_raw" positional list.
    cells: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    mark: Mapped[str | None] = mapped_column(String(64))          # 'F2', 'AHU-1'
    description: Mapped[str | None] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(String(255))
    model_number: Mapped[str | None] = mapped_column(String(255))
    size_label: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    uom: Mapped[UnitOfMeasure | None] = mapped_column(_enum(UnitOfMeasure, "uom_sched"))

    coordinate_space: Mapped[CoordinateSpace] = mapped_column(
        _enum(CoordinateSpace, "coordinate_space"),
        nullable=False,
        default=CoordinateSpace.PDF_POINTS,
    )
    bbox_x0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status_sched"), nullable=False, default=ItemStatus.AUTO
    )

    table: Mapped[ScheduleTable] = relationship(back_populates="rows")

    __table_args__ = (
        UniqueConstraint("schedule_table_id", "row_index", name="uq_schedule_row_index"),
        Index("ix_schedule_row_mark", "pipeline_run_id", "mark"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_sched_row_conf"),
    )


# ---------------------------------------------------------------------------
# Evidence: detections
# ---------------------------------------------------------------------------


class Detection(TimestampMixin, ProvenanceMixin, Base):
    """A symbol found by the detector. Geometry is in ``raster_px``.

    ``render_dpi`` is denormalised from the sheet so the box remains
    interpretable forever, even if a later run re-renders at another DPI.
    ``tile_origin_*`` plus ``tile_size_px`` let us regenerate the exact crop the
    model scored.
    """

    __tablename__ = "detection"

    id: Mapped[uuid.UUID] = pk_column()
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    sheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sheet.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    coordinate_space: Mapped[CoordinateSpace] = mapped_column(
        _enum(CoordinateSpace, "coordinate_space"),
        nullable=False,
        default=CoordinateSpace.RASTER_PX,
    )
    render_dpi: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox_x0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)

    class_name: Mapped[str] = mapped_column(String(128), nullable=False)
    class_id: Mapped[int | None] = mapped_column(Integer)
    discipline: Mapped[Discipline] = mapped_column(
        _enum(Discipline, "discipline_det"), nullable=False, default=Discipline.UNKNOWN
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    weights_sha256: Mapped[str | None] = mapped_column(String(64))
    score_threshold: Mapped[float | None] = mapped_column(Float)
    nms_iou: Mapped[float | None] = mapped_column(Float)
    tile_origin_x: Mapped[int | None] = mapped_column(Integer)
    tile_origin_y: Mapped[int | None] = mapped_column(Integer)
    tile_size_px: Mapped[int | None] = mapped_column(Integer)

    #: NMS keeps the box but records the winner rather than deleting the loser:
    #: "the model saw it and NMS ate it" is a real review question.
    suppressed_by_detection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("detection.id", ondelete="SET NULL")
    )
    #: Set when this symbol was also matched to a nearby text tag, so the
    #: aggregator can dedupe the tagged and detected count of one device.
    matched_text_span_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("text_span.id", ondelete="SET NULL")
    )
    matched_tag: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status_det"), nullable=False, default=ItemStatus.AUTO
    )

    __table_args__ = (
        Index("ix_detection_run_sheet", "pipeline_run_id", "sheet_id"),
        Index("ix_detection_run_class", "pipeline_run_id", "class_name"),
        Index("ix_detection_status", "pipeline_run_id", "status"),
        CheckConstraint("bbox_x1 > bbox_x0 AND bbox_y1 > bbox_y0", name="ck_detection_bbox"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_detection_conf"),
    )


# ---------------------------------------------------------------------------
# Evidence: measurements
# ---------------------------------------------------------------------------


class Measurement(TimestampMixin, ProvenanceMixin, Base):
    """A linear run of conduit / pipe / duct.

    Geometry: ``polyline_points`` is an ordered ``[[x, y], ...]`` list in
    ``pdf_points`` (``srid = 0``, page-local — there is no CRS). ``srid`` is
    carried explicitly so a later PostGIS migration to
    ``geometry(LineString, 0)`` is additive.

    Length model — three numbers, never conflated:
      * ``horizontal_length_ft`` = paper polyline length x scale. Measured.
      * ``vertical_rise_ft``     = INFERRED. Zero unless ``rise_source`` says
        otherwise, and every non-zero value carries a justification.
      * ``total_length_ft``      = horizontal + vertical, stored so exports do
        not re-derive it.
    """

    __tablename__ = "measurement"

    id: Mapped[uuid.UUID] = pk_column()
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    sheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sheet.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    coordinate_space: Mapped[CoordinateSpace] = mapped_column(
        _enum(CoordinateSpace, "coordinate_space"),
        nullable=False,
        default=CoordinateSpace.PDF_POINTS,
    )
    srid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    polyline_points: Mapped[list] = mapped_column(JSONType, nullable=False)
    point_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Axis-aligned envelope of the polyline, for cheap viewport queries.
    bbox_x0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)

    system_type: Mapped[SystemType] = mapped_column(
        _enum(SystemType, "system_type"), nullable=False, default=SystemType.UNKNOWN
    )
    discipline: Mapped[Discipline] = mapped_column(
        _enum(Discipline, "discipline_meas"), nullable=False, default=Discipline.UNKNOWN
    )
    item_class: Mapped[str | None] = mapped_column(String(128))
    size_label: Mapped[str | None] = mapped_column(String(64))     # '3/4"', '12x8'
    size_inches: Mapped[float | None] = mapped_column(Float)       # round systems
    size_width_in: Mapped[float | None] = mapped_column(Float)     # rectangular duct
    size_height_in: Mapped[float | None] = mapped_column(Float)
    #: Which text span gave us the size, if any.
    size_text_span_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("text_span.id", ondelete="SET NULL")
    )

    # --- scale applied ---
    sheet_scale_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sheet_scale.id", ondelete="SET NULL")
    )
    scale_ratio_applied: Mapped[float] = mapped_column(Float, nullable=False)
    paper_length_pt: Mapped[float] = mapped_column(Float, nullable=False)

    # --- the three lengths ---
    horizontal_length_ft: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    vertical_rise_ft: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    total_length_ft: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    rise_source: Mapped[RiseSource] = mapped_column(
        _enum(RiseSource, "rise_source"), nullable=False, default=RiseSource.NONE
    )
    #: Human-readable defence of the rise, e.g. "2 x 9'-0" ceiling height from
    #: note on A-101 + 1'-6" drop to J-box; 2 rise symbols on this run".
    rise_justification: Mapped[str | None] = mapped_column(Text)
    rise_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rise_unit_ft: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    #: Evidence backing the rise, when it came from a note or a symbol.
    rise_evidence_text_span_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("text_span.id", ondelete="SET NULL")
    )
    rise_evidence_detection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("detection.id", ondelete="SET NULL")
    )

    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status_meas"),
        nullable=False,
        default=ItemStatus.AUTO,
    )

    __table_args__ = (
        Index("ix_measurement_run_sheet", "pipeline_run_id", "sheet_id"),
        Index("ix_measurement_run_system_size", "pipeline_run_id", "system_type", "size_label"),
        CheckConstraint("horizontal_length_ft >= 0", name="ck_meas_horiz_nonneg"),
        CheckConstraint("vertical_rise_ft >= 0", name="ck_meas_rise_nonneg"),
        CheckConstraint("scale_ratio_applied > 0", name="ck_meas_scale_positive"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_meas_conf"),
        # A non-zero rise must say where it came from.
        CheckConstraint(
            "vertical_rise_ft = 0 OR rise_source <> 'none'",
            name="ck_meas_rise_justified",
        ),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TakeoffLine(TimestampMixin, Base):
    """The exportable quantity. Written only by the aggregator.

    Versioned, not mutated: re-aggregation inserts ``revision + 1`` and flips
    ``is_current``, so a ``takeoff_line_id`` in a shipped export always
    resolves. ``aggregation_key`` is the deterministic grouping identity
    (discipline | item_class | material | size_label | scope), stable across
    runs, which is what makes a run-to-run diff a plain SQL join.

    **Material is part of the identity.** Without it a 1/2" copper 90 and a
    1/2" PVC 90 share one key, sum into one total, and the estimator has no
    way to see it happened — a wrong number with no visible symptom. The
    component is ``Material.code``, or ``-`` for an item that genuinely has no
    material, so the key is always five fields wide.
    """

    __tablename__ = "takeoff_line"

    id: Mapped[uuid.UUID] = pk_column()
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL = the line rolls up the whole plan set; set = per-sheet line.
    sheet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sheet.id", ondelete="SET NULL"))

    aggregation_key: Mapped[str] = mapped_column(String(512), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("takeoff_line.id", ondelete="SET NULL")
    )

    discipline: Mapped[Discipline] = mapped_column(
        _enum(Discipline, "discipline_line"), nullable=False
    )
    item_class: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: ``Material.code`` from ``conduit.materials`` — never free text, and a
    #: component of ``aggregation_key``. NULL only for items with no material.
    material_code: Mapped[str | None] = mapped_column(String(64))
    size_label: Mapped[str | None] = mapped_column(String(64))
    system_type: Mapped[SystemType | None] = mapped_column(_enum(SystemType, "system_type_line"))
    cost_code: Mapped[str | None] = mapped_column(String(64))

    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    uom: Mapped[UnitOfMeasure] = mapped_column(_enum(UnitOfMeasure, "uom_line"), nullable=False)
    #: Quantity before any human override, kept so the export can show both.
    auto_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    #: How the quantity was arrived at. This is what the export renders as
    #: "factored", "measured" or "counted" in ``Notes / Location`` — a
    #: factored number must never reach an estimator dressed as a counted one.
    #: A column rather than a note, because a reviewer filters on it.
    derivation: Mapped[Derivation] = mapped_column(
        _enum(Derivation, "derivation_line"),
        nullable=False,
        default=Derivation.COUNTED,
        server_default=Derivation.COUNTED.value,
    )
    #: The rule that produced a factored quantity, and the version of that
    #: rule. Both queryable: "which lines used hanger_spacing v1" is a
    #: question a reviewer asks the day a rule turns out to be wrong.
    factor_rule_id: Mapped[str | None] = mapped_column(String(64))
    factor_rule_version: Mapped[str | None] = mapped_column(String(32))
    #: The multiplier actually applied, in the rule's own terms (e.g. 1/7 for
    #: one hanger per 7 ft). Stored so the arithmetic can be re-checked.
    factor_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    #: The rule's remaining parameters, e.g. ``{"spacing_ft": 7.0,
    #: "ends_per_run": 2, "source": "project default"}``. Parameters only —
    #: never the provenance, which is the ``derived_from_line`` evidence row.
    factor_basis: Mapped[dict | None] = mapped_column(JSONType)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status_line"), nullable=False, default=ItemStatus.AUTO
    )
    has_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Sheets that contributed, denormalised for a fast export column.
    contributing_sheet_numbers: Mapped[list | None] = mapped_column(JSONType)
    notes: Mapped[str | None] = mapped_column(Text)

    #: ``foreign_keys`` is explicit because ``takeoff_line_evidence`` now has
    #: two FKs to this table: the line the evidence belongs to, and (for a
    #: factored line) the line it was derived from.
    evidence: Mapped[list[TakeoffLineEvidence]] = relationship(
        back_populates="takeoff_line",
        foreign_keys="TakeoffLineEvidence.takeoff_line_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "pipeline_run_id", "aggregation_key", "revision", name="uq_takeoff_line_key_rev"
        ),
        Index(
            "uq_takeoff_line_current",
            "pipeline_run_id",
            "aggregation_key",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current"),
        ),
        Index("ix_takeoff_line_run_status", "pipeline_run_id", "status"),
        Index("ix_takeoff_line_doc_disc", "document_id", "discipline"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_line_conf"),
        CheckConstraint(
            "evidence_count > 0 OR status = 'manually_added'",
            name="ck_line_requires_evidence",
        ),
        #: A factored line must carry the factor that produced it and the
        #: version of the rule, or the number cannot be re-derived or
        #: challenged. Additive: it constrains only rows that claim to be
        #: factored, and leaves every other shape exactly as it was.
        CheckConstraint(
            "derivation <> 'factored' OR ("
            "factor_rule_id IS NOT NULL AND factor_rule_version IS NOT NULL "
            "AND factor_value IS NOT NULL)",
            name="ck_line_factored_carries_factor",
        ),
    )


class TakeoffLineEvidence(TimestampMixin, Base):
    """Join from an exported quantity to exactly one piece of evidence.

    Exactly one of the six ``*_id`` columns is non-NULL (enforced by
    ``ck_evidence_exactly_one``); ``evidence_kind`` names which. Typed FKs
    rather than a generic ``(kind, uuid)`` pair so the database, not the
    application, guarantees the provenance link resolves.

    The sixth shape, ``source_takeoff_line_id``, is how a *factored* quantity
    cites its basis: hangers from pipe LF, insulation from hot-water LF. The
    allowed set of shapes was widened by exactly one and the "exactly one"
    rule kept — evidence still cannot be absent, which is the whole point of
    the constraint. A cited line carries its own evidence, so following the
    chain still ends at a sheet, a page and a bounding box.
    """

    __tablename__ = "takeoff_line_evidence"

    id: Mapped[uuid.UUID] = pk_column()
    takeoff_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("takeoff_line.id", ondelete="CASCADE"), nullable=False
    )
    evidence_kind: Mapped[EvidenceKind] = mapped_column(
        _enum(EvidenceKind, "evidence_kind"), nullable=False
    )

    detection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("detection.id", ondelete="RESTRICT")
    )
    text_span_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("text_span.id", ondelete="RESTRICT")
    )
    schedule_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schedule_row.id", ondelete="RESTRICT")
    )
    measurement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("measurement.id", ondelete="RESTRICT")
    )
    review_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_action.id", ondelete="RESTRICT")
    )
    #: The line this quantity was factored from. RESTRICT, like every other
    #: evidence FK: you cannot delete the basis out from under a number.
    source_takeoff_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("takeoff_line.id", ondelete="RESTRICT")
    )

    #: How much of the line's quantity this row contributed. Sums to
    #: ``TakeoffLine.auto_quantity`` (pre-override) by construction.
    contribution_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    #: Denormalised for the export Audit worksheet, so producing it is one
    #: query rather than five joins.
    sheet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sheet.id", ondelete="SET NULL"))
    page_number: Mapped[int | None] = mapped_column(Integer)
    coordinate_space: Mapped[CoordinateSpace | None] = mapped_column(
        _enum(CoordinateSpace, "coordinate_space_ev")
    )
    bbox_x0: Mapped[float | None] = mapped_column(Float)
    bbox_y0: Mapped[float | None] = mapped_column(Float)
    bbox_x1: Mapped[float | None] = mapped_column(Float)
    bbox_y1: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    extractor_version: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)

    takeoff_line: Mapped[TakeoffLine] = relationship(
        back_populates="evidence", foreign_keys=[takeoff_line_id]
    )

    __table_args__ = (
        Index("ix_evidence_line", "takeoff_line_id"),
        Index("ix_evidence_detection", "detection_id"),
        Index("ix_evidence_measurement", "measurement_id"),
        Index("ix_evidence_source_line", "source_takeoff_line_id"),
        CheckConstraint(
            "(CASE WHEN detection_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN text_span_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN schedule_row_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN measurement_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN review_action_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN source_takeoff_line_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_evidence_exactly_one",
        ),
        #: A line may not be its own evidence. Cheap to state, and the one
        #: self-citation a factoring bug would produce.
        CheckConstraint(
            "source_takeoff_line_id IS NULL OR source_takeoff_line_id <> takeoff_line_id",
            name="ck_evidence_no_self_basis",
        ),
    )


# ---------------------------------------------------------------------------
# Human review
# ---------------------------------------------------------------------------


class ReviewAction(TimestampMixin, Base):
    """An append-only human decision. Never updated, never deleted.

    ``stable_key`` is a content hash of (sheet, item class, rounded bbox
    centroid, size) so a decision can be replayed onto a later run's evidence
    — estimators will not accept losing an afternoon of corrections to a model
    upgrade. Replayed actions are new rows with ``carried_forward_from_id`` set.
    """

    __tablename__ = "review_action"

    id: Mapped[uuid.UUID] = pk_column()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )

    action_type: Mapped[ReviewActionType] = mapped_column(
        _enum(ReviewActionType, "review_action_type"), nullable=False
    )
    actor_type: Mapped[ActorType] = mapped_column(
        _enum(ActorType, "actor_type_review"), nullable=False, default=ActorType.USER
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Targets: at least one is set. Typed so the FK guarantees resolution.
    takeoff_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("takeoff_line.id", ondelete="SET NULL")
    )
    detection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("detection.id", ondelete="SET NULL")
    )
    measurement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("measurement.id", ondelete="SET NULL")
    )
    schedule_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schedule_row.id", ondelete="SET NULL")
    )
    sheet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sheet.id", ondelete="SET NULL"))

    before_value: Mapped[dict | None] = mapped_column(JSONType)
    after_value: Mapped[dict | None] = mapped_column(JSONType)
    reason: Mapped[str | None] = mapped_column(Text)

    stable_key: Mapped[str | None] = mapped_column(String(128))
    carried_forward_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_action.id", ondelete="SET NULL")
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_review_run_created", "pipeline_run_id", "created_at"),
        Index("ix_review_line", "takeoff_line_id"),
        Index("ix_review_stable_key", "document_id", "stable_key"),
        CheckConstraint(
            "takeoff_line_id IS NOT NULL OR detection_id IS NOT NULL "
            "OR measurement_id IS NOT NULL OR schedule_row_id IS NOT NULL "
            "OR sheet_id IS NOT NULL",
            name="ck_review_has_target",
        ),
    )


# ---------------------------------------------------------------------------
# Export + audit
# ---------------------------------------------------------------------------


class ExportJob(TimestampMixin, Base):
    """A materialised export. Pins the run and freezes the model version set.

    A run cannot be deleted while an ExportJob references it — a shipped
    number must always be reproducible.
    """

    __tablename__ = "export_job"

    id: Mapped[uuid.UUID] = pk_column()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="RESTRICT"), nullable=False
    )

    format: Mapped[ExportFormat] = mapped_column(_enum(ExportFormat, "export_format"),
                                                 nullable=False)
    status: Mapped[ExportStatus] = mapped_column(
        _enum(ExportStatus, "export_status"), nullable=False, default=ExportStatus.QUEUED
    )
    #: Filters applied, e.g. {"disciplines":["E"],"min_confidence":0.5,
    #: "include_rejected":false,"group_by":["discipline","item_class","size"]}
    scope: Mapped[dict | None] = mapped_column(JSONType)
    include_audit_sheet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    object_key: Mapped[str | None] = mapped_column(String(1024))
    manifest_object_key: Mapped[str | None] = mapped_column(String(1024))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    row_count: Mapped[int | None] = mapped_column(Integer)
    evidence_row_count: Mapped[int | None] = mapped_column(Integer)
    #: Frozen at export time; the run's JSONB may move on, this must not.
    model_versions: Mapped[dict | None] = mapped_column(JSONType)
    code_version: Mapped[str | None] = mapped_column(String(64))

    requested_by: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_export_document_created", "document_id", "created_at"),
        Index("ix_export_run", "pipeline_run_id"),
    )


class AuditEvent(Base):
    """Immutable flight recorder. Append-only, never updated or deleted.

    ``seq`` gives total order even when two events share a millisecond. On
    Postgres it is backed by a dedicated sequence; the Alembic migration adds::

        CREATE SEQUENCE audit_event_seq_seq OWNED BY audit_event.seq;
        ALTER TABLE audit_event
            ALTER COLUMN seq SET DEFAULT nextval('audit_event_seq_seq'),
            ALTER COLUMN seq SET NOT NULL;

    It is declared nullable here so the model stays portable to SQLite for
    tests (SQLite has no sequences); the writer helper always supplies a value.
    """

    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = pk_column()
    seq: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project.id", ondelete="SET NULL")
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL")
    )
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="SET NULL")
    )

    #: Table name of the subject, e.g. "takeoff_line". Deliberately a plain
    #: string + uuid (no FK): audit rows must outlive their subjects.
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    #: e.g. "run.created", "detection.batch_inserted", "line.aggregated",
    #: "review.applied", "export.completed", "run.promoted".
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(
        _enum(ActorType, "actor_type_audit"), nullable=False, default=ActorType.SYSTEM
    )
    actor_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict | None] = mapped_column(JSONType)
    request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_document_seq", "document_id", "seq"),
        Index("ix_audit_run_occurred", "pipeline_run_id", "occurred_at"),
    )


__all__ = [
    "Base",
    "utcnow",
    # enums
    "ActorType",
    "ClassificationMethod",
    "CoordinateSpace",
    "Derivation",
    "Discipline",
    "EvidenceKind",
    "ExportFormat",
    "ExportStatus",
    "ItemStatus",
    "ProjectStatus",
    "ReviewActionType",
    "RiseSource",
    "RunStatus",
    "ScaleSource",
    "ScheduleKind",
    "SheetSubtype",
    "SpanRole",
    "StageName",
    "SystemType",
    "TaskStatus",
    "TextSource",
    "UnitOfMeasure",
    # tables
    "AuditEvent",
    "Detection",
    "Document",
    "ExportJob",
    "Measurement",
    "PageTaskState",
    "PipelineRun",
    "Project",
    "ReviewAction",
    "ScheduleRow",
    "ScheduleTable",
    "Sheet",
    "SheetScale",
    "TakeoffLine",
    "TakeoffLineEvidence",
    "TextSpan",
]
