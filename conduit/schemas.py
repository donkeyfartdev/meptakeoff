"""Conduit Takeoff — Pydantic v2 schemas.

These are the wire contract in three places at once: the FastAPI request/response
bodies, the arq task payloads, and the JSON export format. Keeping one set of
models means an evidence row cannot pick up a field on the way from a worker to
the browser without someone noticing.

Enums are imported from ``orm`` so there is exactly one definition of
``Discipline``, ``ItemStatus`` etc. in the codebase.

COORDINATE SPACES: see the module docstring of ``orm.py``. Every geometry-
carrying schema here embeds a :class:`BBox` or :class:`Polyline`, both of which
require an explicit ``space``. There is no way to submit a box without saying
what space it is in.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from orm import (
    ActorType,
    ClassificationMethod,
    CoordinateSpace,
    Derivation,
    Discipline,
    EvidenceKind,
    ExportFormat,
    ExportStatus,
    ItemStatus,
    ProjectStatus,
    ReviewActionType,
    RiseSource,
    RunStatus,
    ScaleSource,
    ScheduleKind,
    SheetSubtype,
    SpanRole,
    StageName,
    SystemType,
    TaskStatus,
    TextSource,
    UnitOfMeasure,
)

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
Quantity = Annotated[Decimal, Field(ge=0)]
PageNumber = Annotated[int, Field(ge=1)]


class Schema(BaseModel):
    """Base: strict about unknown fields, populated from ORM objects."""

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        use_enum_values=False,
        validate_assignment=True,
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class BBox(Schema):
    """Axis-aligned box. ``space`` is mandatory — there is no default frame.

    In ``raster_px`` the origin is top-left, y down, at ``dpi``.
    In ``pdf_points`` the origin is the MediaBox lower-left, y up, and ``dpi``
    must be omitted.
    """

    space: CoordinateSpace
    x0: float
    y0: float
    x1: float
    y1: float
    dpi: int | None = Field(default=None, ge=36, le=1200)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bbox requires x1 >= x0 and y1 >= y0")
        if self.space is CoordinateSpace.RASTER_PX and self.dpi is None:
            raise ValueError("raster_px bbox must carry the dpi it was measured at")
        if self.space is CoordinateSpace.PDF_POINTS and self.dpi is not None:
            raise ValueError("pdf_points bbox is dpi-independent; omit dpi")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def to_pdf_points(self, page: PageGeometry) -> BBox:
        """Convert a raster_px box to pdf_points using the page geometry.

        Only implemented for ``rotation_deg == 0``; rotated pages go through
        ``conduit.geometry.transform.page_transform`` in the real codebase,
        which this method delegates to. Kept here so the design shows the
        conversion is total, not hand-waved.
        """
        if self.space is CoordinateSpace.PDF_POINTS:
            return self
        if page.rotation_deg != 0:
            raise NotImplementedError(
                "rotated-page conversion lives in conduit.geometry.transform"
            )
        s = (self.dpi or page.render_dpi) / 72.0
        return BBox(
            space=CoordinateSpace.PDF_POINTS,
            x0=page.media_box_x0 + self.x0 / s,
            y0=page.media_box_y1 - self.y1 / s,
            x1=page.media_box_x0 + self.x1 / s,
            y1=page.media_box_y1 - self.y0 / s,
        )


class PageGeometry(Schema):
    """Everything needed to move between the two coordinate spaces."""

    media_box_x0: float = 0.0
    media_box_y0: float = 0.0
    media_box_x1: float
    media_box_y1: float
    rotation_deg: Literal[0, 90, 180, 270] = 0
    render_dpi: int = Field(default=200, ge=36, le=1200)
    width_px: int | None = Field(default=None, ge=1)
    height_px: int | None = Field(default=None, ge=1)


class Polyline(Schema):
    """Ordered points in ``pdf_points``. ``srid=0`` means page-local, no CRS."""

    space: Literal[CoordinateSpace.PDF_POINTS] = CoordinateSpace.PDF_POINTS
    srid: int = 0
    points: list[tuple[float, float]] = Field(min_length=2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def paper_length_pt(self) -> float:
        total = 0.0
        for (ax, ay), (bx, by) in zip(self.points, self.points[1:]):
            total += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        return total

    def bbox(self) -> BBox:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return BBox(
            space=CoordinateSpace.PDF_POINTS,
            x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys),
        )


class ScaleSpec(Schema):
    """A drawing scale and where it came from.

    ``scale_ratio`` is dimensionless real-world/paper (1/8" = 1'-0" -> 96).
    ``feet_per_paper_point`` is the derived multiplier used by the measurer and
    is stored, not re-derived, so every consumer agrees.
    """

    scale_text: str | None = None
    scale_ratio: float = Field(gt=0)
    feet_per_paper_point: float | None = Field(default=None, gt=0)
    source: ScaleSource = ScaleSource.UNKNOWN
    confidence: Confidence = 0.0
    viewport_label: str | None = None
    source_text_span_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _derive(self) -> Self:
        if self.feet_per_paper_point is None:
            object.__setattr__(
                self, "feet_per_paper_point", self.scale_ratio / (12.0 * 72.0)
            )
        if self.source is ScaleSource.HUMAN and self.confidence < 1.0:
            object.__setattr__(self, "confidence", 1.0)
        return self


class RiseInference(Schema):
    """The vertical component of a run. Inferred, never measured off the plan.

    A non-zero rise MUST name a source and carry a justification string that an
    estimator can read and argue with. Accuracy of rise inference is unknown
    and unvalidated; see 01-architecture.md §3E.
    """

    vertical_rise_ft: Quantity = Decimal("0")
    source: RiseSource = RiseSource.NONE
    justification: str | None = None
    rise_count: int = Field(default=0, ge=0)
    rise_unit_ft: Decimal | None = Field(default=None, ge=0)
    evidence_text_span_id: uuid.UUID | None = None
    evidence_detection_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _justified(self) -> Self:
        if self.vertical_rise_ft > 0:
            if self.source is RiseSource.NONE:
                raise ValueError("non-zero vertical rise requires a rise source")
            if not self.justification:
                raise ValueError("non-zero vertical rise requires a justification")
        return self


class ProvenanceRef(Schema):
    """The minimum a reviewer needs to find a number on the page."""

    pipeline_run_id: uuid.UUID
    sheet_id: uuid.UUID
    sheet_number: str | None = None
    page_number: PageNumber
    bbox: BBox | None = None
    extractor_version: str
    confidence: Confidence


# ---------------------------------------------------------------------------
# Project / Document / Sheet
# ---------------------------------------------------------------------------


class ProjectCreate(Schema):
    name: str = Field(min_length=1, max_length=255)
    client_name: str | None = None
    location: str | None = None
    bid_due_at: datetime | None = None
    defaults: dict[str, Any] | None = None


class ProjectRead(ProjectCreate):
    id: uuid.UUID
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    created_by: str | None = None
    document_count: int = 0


class DocumentCreate(Schema):
    project_id: uuid.UUID
    name: str
    original_filename: str
    revision_label: str | None = None


class DocumentRead(Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    original_filename: str
    object_key: str
    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(ge=0)
    page_count: int = Field(ge=1)
    revision_label: str | None = None
    current_run_id: uuid.UUID | None = None
    uploaded_by: str | None = None
    created_at: datetime
    deleted_at: datetime | None = None


class UploadAccepted(Schema):
    """Returned synchronously from POST /plansets. No pixels touched yet."""

    document_id: uuid.UUID
    run_id: uuid.UUID
    page_count: int = Field(ge=1)
    sha256: str
    duplicate_of_document_id: uuid.UUID | None = None
    progress_url: str


class SheetClassification(Schema):
    discipline: Discipline
    subtype: SheetSubtype = SheetSubtype.UNKNOWN
    confidence: Confidence
    method: ClassificationMethod
    #: Which rule fired and what it matched, so the number is explainable.
    rationale: dict[str, Any] | None = None


class SheetRead(Schema):
    id: uuid.UUID
    document_id: uuid.UUID
    page_number: PageNumber
    sheet_number: str | None = None
    sheet_title: str | None = None
    classification: SheetClassification
    geometry: PageGeometry
    scale: ScaleSpec | None = None
    raster_object_key: str | None = None
    thumbnail_object_key: str | None = None
    tile_base_key: str | None = None
    tile_max_zoom: int | None = None
    paths_object_key: str | None = None
    has_vector_text: bool = True
    ocr_applied: bool = False
    text_span_count: int = 0


# ---------------------------------------------------------------------------
# Pipeline runs
# ---------------------------------------------------------------------------


class RunCreate(Schema):
    """Start a run. Stages omitted from ``stages`` are inherited from parent."""

    document_id: uuid.UUID
    stages: list[StageName] = Field(min_length=1)
    parent_run_id: uuid.UUID | None = None
    render_dpi: int = Field(default=200, ge=36, le=1200)
    model_versions: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _partial_needs_parent(self) -> Self:
        if StageName.RASTERISE not in self.stages and self.parent_run_id is None:
            raise ValueError(
                "a run that does not rasterise must name a parent_run_id to inherit from"
            )
        return self


class StageProgress(Schema):
    stage: StageName
    pending: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    inherited: int = 0


class RunProgress(Schema):
    """Payload of the SSE ``run.progress`` event and of PipelineRun.progress."""

    run_id: uuid.UUID
    document_id: uuid.UUID
    status: RunStatus
    page_count: int = Field(ge=0)
    pages_done: int = Field(ge=0)
    current_stage: StageName | None = None
    per_stage: list[StageProgress] = Field(default_factory=list)
    failed_pages: list[int] = Field(default_factory=list)
    eta_seconds: int | None = Field(default=None, ge=0)
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def percent(self) -> float:
        return 0.0 if not self.page_count else round(100.0 * self.pages_done / self.page_count, 1)


class PipelineRunRead(Schema):
    id: uuid.UUID
    document_id: uuid.UUID
    parent_run_id: uuid.UUID | None = None
    status: RunStatus
    is_current: bool
    stages_executed: list[StageName]
    inherited_stages: list[StageName] = Field(default_factory=list)
    code_version: str
    config_hash: str | None = None
    model_versions: dict[str, str] = Field(default_factory=dict)
    render_dpi: int
    page_count: int
    progress: RunProgress | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_summary: str | None = None
    created_at: datetime


class PageTaskStateRead(Schema):
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    page_number: PageNumber
    stage: StageName
    status: TaskStatus
    attempts: int = 0
    duration_ms: int | None = None
    peak_rss_mb: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class TextSpanRead(Schema):
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    sheet_id: uuid.UUID
    page_number: PageNumber
    text: str
    normalized_text: str | None = None
    bbox: BBox
    rotation_deg: float = 0.0
    font_name: str | None = None
    font_size_pt: float | None = None
    source: TextSource = TextSource.VECTOR
    confidence: Confidence = 1.0
    role: SpanRole = SpanRole.UNKNOWN
    parsed_value: dict[str, Any] | None = None

    @field_validator("bbox")
    @classmethod
    def _pdf_space(cls, v: BBox) -> BBox:
        if v.space is not CoordinateSpace.PDF_POINTS:
            raise ValueError("text spans are stored in pdf_points")
        return v


class ScheduleRowRead(Schema):
    id: uuid.UUID
    schedule_table_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    sheet_id: uuid.UUID
    page_number: PageNumber
    row_index: int = Field(ge=0)
    cells: dict[str, Any] = Field(default_factory=dict)
    mark: str | None = None
    description: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    size_label: str | None = None
    quantity: Decimal | None = None
    uom: UnitOfMeasure | None = None
    bbox: BBox
    confidence: Confidence = 0.0
    status: ItemStatus = ItemStatus.AUTO


class ScheduleTableRead(Schema):
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    sheet_id: uuid.UUID
    page_number: PageNumber
    title: str | None = None
    kind: ScheduleKind = ScheduleKind.UNKNOWN
    bbox: BBox
    header_labels: list[str] = Field(default_factory=list)
    column_x_positions: list[float] | None = None
    row_count: int = 0
    confidence: Confidence = 0.0
    extractor_version: str
    rows: list[ScheduleRowRead] = Field(default_factory=list)


class DetectionRead(Schema):
    """A symbol detection. ``bbox`` is always ``raster_px`` at ``bbox.dpi``."""

    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    sheet_id: uuid.UUID
    page_number: PageNumber
    bbox: BBox
    class_name: str
    class_id: int | None = None
    discipline: Discipline = Discipline.UNKNOWN
    confidence: Confidence
    model_name: str
    model_version: str
    weights_sha256: str | None = None
    score_threshold: float | None = None
    nms_iou: float | None = None
    tile_origin_x: int | None = None
    tile_origin_y: int | None = None
    tile_size_px: int | None = None
    suppressed_by_detection_id: uuid.UUID | None = None
    matched_text_span_id: uuid.UUID | None = None
    matched_tag: str | None = None
    status: ItemStatus = ItemStatus.AUTO

    @field_validator("bbox")
    @classmethod
    def _raster_space(cls, v: BBox) -> BBox:
        if v.space is not CoordinateSpace.RASTER_PX:
            raise ValueError("detections are stored in raster_px with an explicit dpi")
        return v


class MeasurementRead(Schema):
    """A linear run. Three lengths, never conflated."""

    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    sheet_id: uuid.UUID
    page_number: PageNumber
    polyline: Polyline
    system_type: SystemType = SystemType.UNKNOWN
    discipline: Discipline = Discipline.UNKNOWN
    item_class: str | None = None
    size_label: str | None = None
    size_inches: float | None = Field(default=None, gt=0)
    size_width_in: float | None = Field(default=None, gt=0)
    size_height_in: float | None = Field(default=None, gt=0)
    size_text_span_id: uuid.UUID | None = None
    scale: ScaleSpec
    sheet_scale_id: uuid.UUID | None = None
    horizontal_length_ft: Quantity
    rise: RiseInference = Field(default_factory=RiseInference)
    extractor_version: str
    confidence: Confidence = 0.0
    status: ItemStatus = ItemStatus.AUTO

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_length_ft(self) -> Decimal:
        return self.horizontal_length_ft + self.rise.vertical_rise_ft

    @model_validator(mode="after")
    def _rise_needs_review(self) -> Self:
        """An inferred rise is never silently trusted."""
        if (
            self.rise.vertical_rise_ft > 0
            and self.rise.source
            in (RiseSource.PROJECT_DEFAULT, RiseSource.CEILING_HEIGHT_NOTE)
            and self.status is ItemStatus.AUTO
        ):
            object.__setattr__(self, "status", ItemStatus.NEEDS_REVIEW)
        return self


# ---------------------------------------------------------------------------
# Aggregation + export
# ---------------------------------------------------------------------------


#: Which FK column each evidence kind must populate.
EVIDENCE_KIND_FIELD: dict[EvidenceKind, str] = {
    EvidenceKind.DETECTION: "detection_id",
    EvidenceKind.TEXT_SPAN: "text_span_id",
    EvidenceKind.SCHEDULE_ROW: "schedule_row_id",
    EvidenceKind.MEASUREMENT: "measurement_id",
    EvidenceKind.MANUAL: "review_action_id",
    EvidenceKind.DERIVED_FROM_LINE: "source_takeoff_line_id",
}


#: The derivation words that reach the estimator, in the ``Notes / Location``
#: column. Kept next to the enum so the label cannot drift from the value.
DERIVATION_LABEL: dict[Derivation, str] = {
    Derivation.COUNTED: "counted",
    Derivation.MEASURED: "measured",
    Derivation.DERIVED_GEOMETRIC: "derived: geometry",
    Derivation.FACTORED: "factored",
    Derivation.MANUAL: "entered by reviewer",
}


def derivation_label(
    derivation: Derivation,
    *,
    factor_rule_id: str | None = None,
    factor_rule_version: str | None = None,
) -> str:
    """The derivation segment of ``Notes / Location``.

    A factored quantity says so, and names the rule, in the column the
    estimator actually reads — ``factored: hanger_spacing v1``. Presenting a
    factored number as a counted one is the failure this exists to prevent, so
    the label is generated from the stored column rather than written by a
    caller who might forget.
    """
    if derivation is not Derivation.FACTORED:
        return DERIVATION_LABEL[derivation]
    rule = factor_rule_id or "unnamed rule"
    version = f" {factor_rule_version}" if factor_rule_version else ""
    return f"factored: {rule}{version}"


class EvidenceRef(Schema):
    """One contributor to a takeoff line. Exactly one id field is set."""

    id: uuid.UUID
    evidence_kind: EvidenceKind
    detection_id: uuid.UUID | None = None
    text_span_id: uuid.UUID | None = None
    schedule_row_id: uuid.UUID | None = None
    measurement_id: uuid.UUID | None = None
    review_action_id: uuid.UUID | None = None
    #: The line a factored quantity was derived from. The sixth allowed shape.
    source_takeoff_line_id: uuid.UUID | None = None
    contribution_qty: Decimal = Decimal("0")
    sheet_id: uuid.UUID | None = None
    sheet_number: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    bbox: BBox | None = None
    confidence: Confidence | None = None
    extractor_version: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        set_fields = [
            f
            for f in (
                "detection_id",
                "text_span_id",
                "schedule_row_id",
                "measurement_id",
                "review_action_id",
                "source_takeoff_line_id",
            )
            if getattr(self, f) is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                f"evidence must reference exactly one source, got {set_fields or 'none'}"
            )
        expected = EVIDENCE_KIND_FIELD[self.evidence_kind]
        if set_fields[0] != expected:
            raise ValueError(
                f"evidence_kind={self.evidence_kind.value} requires {expected}, "
                f"got {set_fields[0]}"
            )
        return self


class TakeoffLineRead(Schema):
    """The exportable quantity. Cannot exist without evidence or a manual add."""

    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    project_id: uuid.UUID
    document_id: uuid.UUID
    sheet_id: uuid.UUID | None = None
    aggregation_key: str
    revision: int = Field(default=1, ge=1)
    is_current: bool = True
    supersedes_id: uuid.UUID | None = None
    discipline: Discipline
    item_class: str
    description: str | None = None
    material_code: str | None = None
    size_label: str | None = None
    system_type: SystemType | None = None
    cost_code: str | None = None
    quantity: Quantity
    uom: UnitOfMeasure
    auto_quantity: Decimal | None = None
    derivation: Derivation = Derivation.COUNTED
    factor_rule_id: str | None = None
    factor_rule_version: str | None = None
    factor_value: Decimal | None = None
    factor_basis: dict | None = None
    confidence: Confidence = 0.0
    status: ItemStatus = ItemStatus.AUTO
    has_override: bool = False
    contributing_sheet_numbers: list[str] = Field(default_factory=list)
    notes: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_evidence(self) -> Self:
        if not self.evidence and self.status is not ItemStatus.MANUALLY_ADDED:
            raise ValueError(
                "a takeoff line with no evidence must have status=manually_added"
            )
        return self

    @model_validator(mode="after")
    def _factored_carries_its_factor(self) -> Self:
        """Mirrors ``ck_line_factored_carries_factor``.

        A factored number that does not carry the rule and version that
        produced it cannot be re-derived or challenged, which makes it exactly
        the unauditable number this schema refuses to hold.
        """
        if self.derivation is Derivation.FACTORED and not (
            self.factor_rule_id and self.factor_rule_version and self.factor_value is not None
        ):
            raise ValueError(
                "derivation=factored requires factor_rule_id, factor_rule_version "
                "and factor_value"
            )
        return self

    @property
    def derivation_note(self) -> str:
        """What the estimator reads: ``counted``, ``measured``, ``factored: …``."""
        return derivation_label(
            self.derivation,
            factor_rule_id=self.factor_rule_id,
            factor_rule_version=self.factor_rule_version,
        )


class TakeoffQuery(Schema):
    """Re-query a processed plan set. Never re-runs the pipeline."""

    document_id: uuid.UUID
    run_id: uuid.UUID | None = None          # None -> the document's current run
    disciplines: list[Discipline] | None = None
    sheet_ids: list[uuid.UUID] | None = None
    system_types: list[SystemType] | None = None
    statuses: list[ItemStatus] | None = None
    min_confidence: Confidence = 0.0
    include_evidence: bool = False
    group_by: list[
        Literal["discipline", "item_class", "material", "size_label", "sheet"]
    ] = Field(
        default_factory=lambda: ["discipline", "item_class", "material", "size_label"]
    )
    limit: int = Field(default=500, ge=1, le=5000)
    offset: int = Field(default=0, ge=0)


class TakeoffPage(Schema):
    run_id: uuid.UUID
    is_current_run: bool
    total: int
    lines: list[TakeoffLineRead]


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class ReviewActionCreate(Schema):
    document_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    action_type: ReviewActionType
    takeoff_line_id: uuid.UUID | None = None
    detection_id: uuid.UUID | None = None
    measurement_id: uuid.UUID | None = None
    schedule_row_id: uuid.UUID | None = None
    sheet_id: uuid.UUID | None = None
    after_value: dict[str, Any] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _has_target(self) -> Self:
        if not any(
            (
                self.takeoff_line_id,
                self.detection_id,
                self.measurement_id,
                self.schedule_row_id,
                self.sheet_id,
            )
        ):
            raise ValueError("a review action must target something")
        mutating = {
            ReviewActionType.EDIT_QUANTITY,
            ReviewActionType.EDIT_CLASS,
            ReviewActionType.EDIT_SIZE,
            ReviewActionType.EDIT_GEOMETRY,
            ReviewActionType.ADD_MANUAL,
            ReviewActionType.SET_SCALE,
            ReviewActionType.SET_RISE,
            ReviewActionType.RECLASSIFY_SHEET,
        }
        if self.action_type in mutating and not self.after_value:
            raise ValueError(f"{self.action_type.value} requires after_value")
        return self


class ReviewActionRead(Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    document_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    action_type: ReviewActionType
    actor_type: ActorType = ActorType.USER
    actor_id: str
    takeoff_line_id: uuid.UUID | None = None
    detection_id: uuid.UUID | None = None
    measurement_id: uuid.UUID | None = None
    schedule_row_id: uuid.UUID | None = None
    sheet_id: uuid.UUID | None = None
    before_value: dict[str, Any] | None = None
    after_value: dict[str, Any] | None = None
    reason: str | None = None
    stable_key: str | None = None
    carried_forward_from_id: uuid.UUID | None = None
    created_at: datetime
    applied_at: datetime | None = None


# ---------------------------------------------------------------------------
# Export + audit
# ---------------------------------------------------------------------------


class ExportRequest(Schema):
    document_id: uuid.UUID
    run_id: uuid.UUID | None = None
    format: ExportFormat = ExportFormat.XLSX
    scope: TakeoffQuery | None = None
    include_audit_sheet: bool = True


class ExportJobRead(Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    document_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    format: ExportFormat
    status: ExportStatus
    scope: dict[str, Any] | None = None
    include_audit_sheet: bool = True
    object_key: str | None = None
    manifest_object_key: str | None = None
    checksum_sha256: str | None = None
    byte_size: int | None = None
    row_count: int | None = None
    evidence_row_count: int | None = None
    model_versions: dict[str, str] | None = None
    code_version: str | None = None
    requested_by: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    download_url: str | None = None


class AuditEventRead(Schema):
    id: uuid.UUID
    seq: int
    occurred_at: datetime
    project_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    pipeline_run_id: uuid.UUID | None = None
    entity_type: str
    entity_id: uuid.UUID | None = None
    event_type: str
    actor_type: ActorType = ActorType.SYSTEM
    actor_id: str | None = None
    payload: dict[str, Any] | None = None
    request_id: str | None = None


class AuditTrail(Schema):
    """The full provenance answer for one exported number.

    ``GET /takeoff-lines/{id}/audit`` returns this, and it is the same shape
    serialised into the export manifest and the XLSX Audit worksheet.
    """

    takeoff_line: TakeoffLineRead
    run: PipelineRunRead
    evidence: list[EvidenceRef]
    provenance: list[ProvenanceRef]
    review_actions: list[ReviewActionRead] = Field(default_factory=list)
    exported_in: list[uuid.UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Worker task payloads (arq)
# ---------------------------------------------------------------------------


class PageTask(Schema):
    """Envelope for every per-page worker task."""

    pipeline_run_id: uuid.UUID
    document_id: uuid.UUID
    sheet_id: uuid.UUID
    page_number: PageNumber
    stage: StageName
    render_dpi: int = Field(default=200, ge=36, le=1200)
    options: dict[str, Any] | None = None


class AggregateTask(Schema):
    pipeline_run_id: uuid.UUID
    document_id: uuid.UUID
    replay_review_actions: bool = True
    parent_run_id: uuid.UUID | None = None


__all__ = [
    "AggregateTask",
    "AuditEventRead",
    "AuditTrail",
    "BBox",
    "Confidence",
    "DERIVATION_LABEL",
    "derivation_label",
    "DetectionRead",
    "DocumentCreate",
    "DocumentRead",
    "EvidenceRef",
    "ExportJobRead",
    "ExportRequest",
    "MeasurementRead",
    "PageGeometry",
    "PageTask",
    "PageTaskStateRead",
    "PipelineRunRead",
    "Polyline",
    "ProjectCreate",
    "ProjectRead",
    "ProvenanceRef",
    "Quantity",
    "ReviewActionCreate",
    "ReviewActionRead",
    "RiseInference",
    "RunCreate",
    "RunProgress",
    "ScaleSpec",
    "Schema",
    "ScheduleRowRead",
    "ScheduleTableRead",
    "SheetClassification",
    "SheetRead",
    "StageProgress",
    "TakeoffLineRead",
    "TakeoffPage",
    "TakeoffQuery",
    "TextSpanRead",
    "UploadAccepted",
]
