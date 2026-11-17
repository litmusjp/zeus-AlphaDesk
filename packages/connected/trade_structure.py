"""Structure classification and gating for the standalone trade assessment.

Pure functions with no assessment imports, so the API route and the evaluator make the SAME
decision (one gate), and the engine's eight structure names are the only vocabulary.

Dark release: `AssessmentPolicy.allowed_structures` defaults to the four shapes that were
assessable before. A shape that classifies but is not allowed is treated exactly like an
unknown shape (the caller sees today's `unsupported_strategy` receipt, no provider access).
"""

from __future__ import annotations

import re
from typing import Any

from packages.domain.options import StructureType
from packages.domain.workflow import Direction

_OCC = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")

# Enum order is the canonical order of the allowlist (stable policy snapshots).
DEFAULT_ALLOWED_STRUCTURES: tuple[str, ...] = (
    StructureType.LONG_CALL.value,
    StructureType.LONG_PUT.value,
    StructureType.BULL_CALL_DEBIT_SPREAD.value,
    StructureType.BEAR_PUT_DEBIT_SPREAD.value,
)
STRUCTURE_ORDER: tuple[str, ...] = tuple(member.value for member in StructureType)

CREDIT_STRUCTURES = frozenset(
    {StructureType.BULL_PUT_CREDIT_SPREAD, StructureType.BEAR_CALL_CREDIT_SPREAD}
)
PROTECTIVE_STRUCTURES = frozenset(
    {StructureType.PROTECTIVE_PUT, StructureType.PROTECTIVE_PUT_SPREAD}
)

# The one authoritative direction for each structure. Protective structures are not directional
# bets: they are not catalyst-scored and have no signal-direction check.
STRUCTURE_DIRECTION: dict[StructureType, Direction | None] = {
    StructureType.LONG_CALL: Direction.BULLISH,
    StructureType.BULL_CALL_DEBIT_SPREAD: Direction.BULLISH,
    StructureType.BULL_PUT_CREDIT_SPREAD: Direction.BULLISH,
    StructureType.LONG_PUT: Direction.BEARISH,
    StructureType.BEAR_PUT_DEBIT_SPREAD: Direction.BEARISH,
    StructureType.BEAR_CALL_CREDIT_SPREAD: Direction.BEARISH,
    StructureType.PROTECTIVE_PUT: None,
    StructureType.PROTECTIVE_PUT_SPREAD: None,
}


def structure_direction(structure: StructureType) -> Direction | None:
    return STRUCTURE_DIRECTION[structure]


def price_kind(structure: StructureType) -> str:
    """`credit` when the limit price is a net credit received, otherwise a net debit paid."""
    return "credit" if structure in CREDIT_STRUCTURES else "debit"


def classify_shape(trade: Any) -> StructureType | None:
    """Which of the eight engine structures the request is, or None if it is none of them."""
    matches = [_OCC.fullmatch(leg.symbol) for leg in trade.legs]
    if any(match is None for match in matches):
        return None
    parsed = [match for match in matches if match is not None]
    if any(leg.ratio_quantity != 1 for leg in trade.legs):
        return None
    stock = getattr(trade, "stock_context", None)
    if len(trade.legs) == 1:
        leg, option = trade.legs[0], parsed[0]
        if leg.side != "buy":
            return None
        if stock is not None:
            return StructureType.PROTECTIVE_PUT if option[3] == "P" else None
        return StructureType.LONG_CALL if option[3] == "C" else StructureType.LONG_PUT
    if len({leg.symbol for leg in trade.legs}) != 2:
        return None
    if len({(m[1], m[2], m[3]) for m in parsed}) != 1:
        return None
    long = next((m for leg, m in zip(trade.legs, parsed, strict=True) if leg.side == "buy"), None)
    short = next((m for leg, m in zip(trade.legs, parsed, strict=True) if leg.side == "sell"), None)
    if long is None or short is None:
        return None
    long_strike, short_strike = int(long[4]), int(short[4])
    if long[3] == "C":
        if stock is not None:
            return None
        if long_strike < short_strike:
            return StructureType.BULL_CALL_DEBIT_SPREAD
        return StructureType.BEAR_CALL_CREDIT_SPREAD if long_strike > short_strike else None
    if long_strike > short_strike:
        return (
            StructureType.PROTECTIVE_PUT_SPREAD
            if stock is not None
            else StructureType.BEAR_PUT_DEBIT_SPREAD
        )
    if stock is not None:
        return None
    return StructureType.BULL_PUT_CREDIT_SPREAD if long_strike < short_strike else None


def gate_structure(trade: Any, policy: Any) -> StructureType | None:
    """The single decision used by the API route (before any provider access) and the evaluator.

    Returns the structure to assess, or None when the shape is unknown OR not enabled.
    """
    structure = classify_shape(trade)
    if structure is None or structure.value not in policy.allowed_structures:
        return None
    return structure
