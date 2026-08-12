"""PROPOSED — PENDING OWNER CONFIRMATION. Electrical and HVAC wording.

===========================================================================
NOTHING IN THIS FILE HAS BEEN CONFIRMED BY AN ESTIMATOR OR BY THE OWNER.
===========================================================================

The owner supplied a plumbing takeoff workbook, so the plumbing vocabulary in
``vocabulary.py`` is seeded from words a real estimator wrote. For electrical
and HVAC we have no such source. What follows is a defensible starting point
assembled from ordinary trade usage — it is a **proposal**, not a decision,
and every entry is tagged ``VocabStatus.PROPOSED_PENDING_OWNER``.

This is deliberately one file so replacing it is a single edit. When the owner
supplies the real words:

1. Rewrite the two tuples below. Codes may change freely — nothing has been
   exported yet, and once something has, ``VOCABULARY_VERSION`` in
   ``vocabulary.py`` gets bumped and the old key stays resolvable.
2. Move the ``VocabStatus`` on each entry to ``OWNER_SOURCED``.
3. ``pending_owner_confirmation()`` should then return only the shared
   plumbing entries that also carry an E/M trade (``PVC_SCH40``, ``TEE``,
   ``ELBOW_90`` …) — those live in ``vocabulary.py`` because the *word* is
   plumbing-sourced, and they are listed by that function too, so the owner's
   review list is complete regardless of which file an entry sits in.

Specific things we are guessing at and would rather be told (see
``docs/output-schema.md`` §8, open questions):

* Electrical: does the estimator write ``LB``, ``condulet``, ``Type LB body``?
  Are conduit couplings and connectors counted at all, or carried in a
  per-100-ft allowance? Is wire taken off by conductor-foot or by
  circuit-foot with a conductor count?
* HVAC: is duct taken off by the pound (fabricated weight), the linear foot,
  or the square foot of sheet metal? All three are in use, and the answer
  changes the ``Unit`` column, not just the wording. Are duct fittings counted
  individually or folded into a fabrication allowance?

No accuracy or coverage claim is made for this list. It is a vocabulary, not
a model.
"""

from __future__ import annotations

from conduit.db.models import Discipline
from conduit.materials.sizes import SizeKind
from conduit.materials.vocabulary import (
    ItemCategory,
    ItemType,
    Material,
    VocabStatus,
)

__all__ = ["ITEM_TYPES", "MATERIALS", "SHARED_ENTRIES_ALSO_PROPOSED"]

_E = Discipline.ELECTRICAL
_M = Discipline.MECHANICAL
_PROPOSED = VocabStatus.PROPOSED_PENDING_OWNER

#: Entries that physically live in ``vocabulary.py`` (because the word came
#: from the plumbing workbook) but are *also* offered to electrical or
#: mechanical, and are therefore equally unconfirmed for those trades. Listed
#: here only for the human reader; ``pending_owner_confirmation()`` derives the
#: authoritative list from the registry data.
SHARED_ENTRIES_ALSO_PROPOSED: tuple[str, ...] = (
    "PVC_SCH40", "STAINLESS", "GALV_STEEL", "FIBERGLASS",
    "ELBOW_90", "ELBOW_45", "TEE", "COUPLING", "REDUCER", "BUSHING", "CAP",
    "INSULATION", "HANGER", "ALLOWANCE",
)


MATERIALS: tuple[Material, ...] = (
    # --- electrical raceway and conductor ---
    Material("EMT", "EMT", ((_E, _PROPOSED),),
             aliases=("electrical metallic tubing", "thinwall")),
    Material("IMC", "IMC", ((_E, _PROPOSED),), aliases=("intermediate metal conduit",)),
    Material("RMC", "rigid", ((_E, _PROPOSED),),
             aliases=("rigid metal conduit", "grc", "galvanized rigid conduit")),
    Material("FMC", "flex", ((_E, _PROPOSED),), aliases=("flexible metal conduit", "greenfield")),
    Material("LFMC", "liquidtight", ((_E, _PROPOSED),),
             aliases=("lfmc", "sealtite", "liquid tight flex")),
    Material("MC_CABLE", "MC cable", ((_E, _PROPOSED),), aliases=("metal clad cable",)),
    Material("CU_THHN", "THHN copper", ((_E, _PROPOSED),),
             aliases=("thhn", "thhn/thwn copper", "cu thhn")),
    Material("AL_XHHW", "XHHW aluminum", ((_E, _PROPOSED),),
             aliases=("xhhw al", "aluminum xhhw")),
    Material("CU_BARE", "bare copper", ((_E, _PROPOSED),), aliases=("bare cu", "bare ground")),
    Material("STEEL_STRUT", "strut", ((_E, _PROPOSED), (_M, _PROPOSED)),
             aliases=("unistrut", "channel strut")),
    # --- HVAC ---
    Material("ALUMINUM", "aluminum", ((_M, _PROPOSED),), aliases=("aluminium", "alum")),
    Material("DUCTBOARD", "ductboard", ((_M, _PROPOSED),), aliases=("duct board",)),
    Material("FLEX_INSULATED", "insulated flex", ((_M, _PROPOSED),),
             aliases=("insulated flexible duct",)),
    Material("ACR_COPPER", "ACR copper", ((_M, _PROPOSED),), aliases=("refrigerant copper",)),
    Material("ARMAFLEX", "elastomeric", ((_M, _PROPOSED),),
             aliases=("armaflex", "closed cell insulation")),
)


ITEM_TYPES: tuple[ItemType, ...] = (
    # --- electrical: linear ---
    ItemType("CONDUIT", "conduit", ItemCategory.CONDUIT, ((_E, _PROPOSED),)),
    ItemType("WIRE", "wire", ItemCategory.WIRE, ((_E, _PROPOSED),),
             aliases=("conductor", "branch wire"), size_kind=SizeKind.WIRE_GAUGE),
    ItemType("CABLE_TRAY", "cable tray", ItemCategory.CONDUIT, ((_E, _PROPOSED),),
             aliases=("tray",)),
    # --- electrical: raceway fittings ---
    ItemType("CONNECTOR", "connector", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("conduit connector", "set screw connector")),
    ItemType("CONDULET_LB", "LB", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("lb condulet", "type lb", "lb body")),
    ItemType("CONDULET_LL", "LL", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("ll condulet", "type ll")),
    ItemType("CONDULET_LR", "LR", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("lr condulet", "type lr")),
    ItemType("CONDULET_T", "T condulet", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("type t condulet",)),
    ItemType("CONDULET_C", "C condulet", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("type c condulet",)),
    ItemType("EXPANSION_FITTING", "expansion fitting", ItemCategory.FITTING,
             ((_E, _PROPOSED),), aliases=("expansion coupling",)),
    ItemType("GROUND_BUSHING", "ground bushing", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("grounding bushing",)),
    ItemType("LOCKNUT", "locknut", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("lock nut",)),
    ItemType("CONDUIT_STRAP", "conduit strap", ItemCategory.HANGER, ((_E, _PROPOSED),),
             aliases=("one hole strap", "two hole strap")),
    ItemType("WEATHERHEAD", "weatherhead", ItemCategory.FITTING, ((_E, _PROPOSED),),
             aliases=("service head",)),
    # --- electrical: boxes ---
    ItemType("BOX_4SQ", "4-square box", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("4 square box", "4s box")),
    ItemType("MUD_RING", "mud ring", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("plaster ring",)),
    ItemType("JUNCTION_BOX", "junction box", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("j box", "jbox")),
    ItemType("PULL_BOX", "pull box", ItemCategory.DEVICE, ((_E, _PROPOSED),)),
    # --- electrical: devices ---
    ItemType("RECEPTACLE_DUPLEX", "duplex receptacle", ItemCategory.DEVICE,
             ((_E, _PROPOSED),), aliases=("duplex recep", "receptacle", "outlet")),
    ItemType("RECEPTACLE_GFCI", "GFCI receptacle", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("gfci", "gfi receptacle")),
    ItemType("RECEPTACLE_QUAD", "quad receptacle", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("quad recep",)),
    ItemType("SWITCH_SP", "single-pole switch", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("sp switch", "single pole switch", "switch")),
    ItemType("SWITCH_3WAY", "3-way switch", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("three way switch", "3 way switch")),
    ItemType("SWITCH_4WAY", "4-way switch", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("four way switch", "4 way switch")),
    ItemType("DIMMER", "dimmer", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("dimmer switch",)),
    ItemType("OCCUPANCY_SENSOR", "occupancy sensor", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("occ sensor", "vacancy sensor")),
    ItemType("DATA_OUTLET", "data outlet", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("data jack", "comm outlet")),
    # --- electrical: equipment ---
    ItemType("LIGHT_FIXTURE", "light fixture", ItemCategory.EQUIPMENT, ((_E, _PROPOSED),),
             aliases=("luminaire", "fixture")),
    ItemType("EXIT_SIGN", "exit sign", ItemCategory.EQUIPMENT, ((_E, _PROPOSED),)),
    ItemType("EMERGENCY_LIGHT", "emergency light", ItemCategory.EQUIPMENT, ((_E, _PROPOSED),),
             aliases=("em light", "battery unit")),
    ItemType("PANELBOARD", "panelboard", ItemCategory.EQUIPMENT, ((_E, _PROPOSED),),
             aliases=("panel",)),
    ItemType("DISCONNECT", "disconnect", ItemCategory.EQUIPMENT, ((_E, _PROPOSED),),
             aliases=("safety switch", "disco")),
    ItemType("TRANSFORMER", "transformer", ItemCategory.EQUIPMENT, ((_E, _PROPOSED),),
             aliases=("xfmr",)),
    ItemType("SMOKE_DETECTOR", "smoke detector", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("smoke det",)),
    ItemType("PULL_STATION", "pull station", ItemCategory.DEVICE, ((_E, _PROPOSED),),
             aliases=("manual pull station",)),
    # --- HVAC: linear ---
    ItemType("DUCT", "duct", ItemCategory.DUCT, ((_M, _PROPOSED),), aliases=("ductwork",)),
    ItemType("FLEX_DUCT", "flex duct", ItemCategory.DUCT, ((_M, _PROPOSED),),
             aliases=("flexible duct",)),
    ItemType("DUCT_LINER", "duct liner", ItemCategory.INSULATION, ((_M, _PROPOSED),),
             aliases=("liner",)),
    ItemType("DUCT_WRAP", "duct wrap", ItemCategory.INSULATION, ((_M, _PROPOSED),),
             aliases=("external wrap",)),
    # --- HVAC: duct fittings ---
    ItemType("DUCT_ELBOW_RADIUS", "radius elbow", ItemCategory.DUCT_FITTING,
             ((_M, _PROPOSED),), aliases=("radius ell",)),
    ItemType("DUCT_ELBOW_MITERED", "mitered elbow", ItemCategory.DUCT_FITTING,
             ((_M, _PROPOSED),), aliases=("square throat elbow",)),
    ItemType("TURNING_VANES", "turning vanes", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("vanes", "vane set")),
    ItemType("DUCT_TRANSITION", "transition", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("duct transition",), size_kind=SizeKind.REDUCER),
    ItemType("DUCT_OFFSET", "offset", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("duct offset",)),
    ItemType("DUCT_TAKEOFF_45", "45 takeoff", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("45 degree takeoff",)),
    ItemType("CONICAL_TAP", "conical tap", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("conical spin in",)),
    ItemType("SPIN_IN", "spin-in", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("spin in fitting",)),
    ItemType("DUCT_END_CAP", "end cap", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("duct cap",)),
    ItemType("FLEX_CONNECTOR", "flex connector", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("canvas connector", "vibration connector")),
    ItemType("VOLUME_DAMPER", "volume damper", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),),
             aliases=("manual volume damper", "vd damper")),
    ItemType("FIRE_DAMPER", "fire damper", ItemCategory.DUCT_FITTING, ((_M, _PROPOSED),)),
    ItemType("FIRE_SMOKE_DAMPER", "fire/smoke damper", ItemCategory.DUCT_FITTING,
             ((_M, _PROPOSED),), aliases=("fsd", "combination fire smoke damper")),
    # --- HVAC: air terminals and equipment ---
    ItemType("DIFFUSER", "diffuser", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("supply diffuser",)),
    ItemType("RETURN_GRILLE", "return grille", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("return grill", "rag")),
    ItemType("REGISTER", "register", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),)),
    ItemType("LOUVER", "louver", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("louvre",)),
    ItemType("AHU", "air handling unit", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("ahu",)),
    ItemType("RTU", "rooftop unit", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("rtu",)),
    ItemType("VAV_BOX", "VAV box", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("vav", "terminal box")),
    ItemType("FAN_COIL", "fan coil unit", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("fcu",)),
    ItemType("EXHAUST_FAN", "exhaust fan", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("ef",)),
    ItemType("CONDENSING_UNIT", "condensing unit", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("cu unit", "outdoor unit")),
    ItemType("SPLIT_SYSTEM", "split system", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),)),
    ItemType("THERMOSTAT", "thermostat", ItemCategory.EQUIPMENT, ((_M, _PROPOSED),),
             aliases=("tstat",)),
)
