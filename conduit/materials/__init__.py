"""Controlled vocabulary for estimator-facing material names.

``conduit.materials`` is an **interface**, not a convenience. The owner prices
from their own pricebook, so our line items have to join to it; a generated
name from a closed vocabulary joins, and free text does not.

Read ``docs/output-schema.md`` before changing anything here.
"""

from conduit.materials.sizes import Size, SizeKind, format_inches, parse_size
from conduit.materials.vocabulary import (
    AMBIGUOUS_TERMS,
    DOC_SCOPE,
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
    ParsedItemName,
    TermKind,
    UnderSpecifiedTerm,
    VocabStatus,
    aggregation_key,
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
    sheet_scope,
)

__all__ = [
    "AMBIGUOUS_TERMS",
    "DOC_SCOPE",
    "FAMILIES",
    "ITEM_TYPES",
    "MATERIALS",
    "VOCABULARY_VERSION",
    "WEIGHT_CATEGORIES",
    "AmbiguousTerm",
    "Family",
    "ItemCategory",
    "ItemType",
    "Material",
    "ParsedItemName",
    "Size",
    "SizeKind",
    "TermKind",
    "UnderSpecifiedTerm",
    "VocabStatus",
    "aggregation_key",
    "default_uom",
    "family_members",
    "format_inches",
    "item_key",
    "parse_item_name",
    "parse_size",
    "pending_owner_confirmation",
    "render_item_name",
    "resolve_family",
    "resolve_item_type",
    "resolve_material",
    "resolve_term",
    "resolve_unit",
    "sheet_scope",
]
