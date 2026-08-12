"""Controlled vocabulary for estimator-facing material names.

``conduit.materials`` is an **interface**, not a convenience. The owner prices
from their own pricebook, so our line items have to join to it; a generated
name from a closed vocabulary joins, and free text does not.

Read ``docs/output-schema.md`` before changing anything here.
"""

from conduit.materials.sizes import Size, SizeKind, format_inches, parse_size
from conduit.materials.vocabulary import (
    DOC_SCOPE,
    ITEM_TYPES,
    MATERIALS,
    VOCABULARY_VERSION,
    ItemCategory,
    ItemType,
    Material,
    ParsedItemName,
    VocabStatus,
    aggregation_key,
    default_uom,
    item_key,
    parse_item_name,
    pending_owner_confirmation,
    render_item_name,
    resolve_item_type,
    resolve_material,
    sheet_scope,
)

__all__ = [
    "DOC_SCOPE",
    "ITEM_TYPES",
    "MATERIALS",
    "VOCABULARY_VERSION",
    "ItemCategory",
    "ItemType",
    "Material",
    "ParsedItemName",
    "Size",
    "SizeKind",
    "VocabStatus",
    "aggregation_key",
    "default_uom",
    "format_inches",
    "item_key",
    "parse_item_name",
    "parse_size",
    "pending_owner_confirmation",
    "render_item_name",
    "resolve_item_type",
    "resolve_material",
    "sheet_scope",
]
