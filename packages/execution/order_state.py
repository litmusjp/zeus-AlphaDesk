from __future__ import annotations

from decimal import Decimal
from enum import StrEnum


class BrokerFillState(StrEnum):
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    BROKER_REJECTED = "BROKER_REJECTED"


_KNOWN_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "done_for_day",
    "partially_filled",
    "filled",
    "rejected",
    "canceled",
    "expired",
    "replaced",
}


def broker_fill_state(
    status: str, filled_quantity: Decimal, quantity: Decimal
) -> BrokerFillState | None:
    """Validate provider status/fill consistency and return the local outcome."""
    normalized_status = status.lower()
    if normalized_status not in _KNOWN_STATUSES:
        return None
    if (
        not quantity.is_finite()
        or quantity <= 0
        or not filled_quantity.is_finite()
        or not Decimal("0") <= filled_quantity <= quantity
    ):
        return None
    if normalized_status in {"new", "accepted", "pending_new"}:
        return BrokerFillState.SUBMITTED if filled_quantity == 0 else None
    if normalized_status == "done_for_day":
        if filled_quantity == 0:
            return BrokerFillState.SUBMITTED
        if filled_quantity == quantity:
            return BrokerFillState.FILLED
        return BrokerFillState.PARTIALLY_FILLED
    if normalized_status == "partially_filled":
        return BrokerFillState.PARTIALLY_FILLED if 0 < filled_quantity < quantity else None
    if normalized_status == "filled":
        return BrokerFillState.FILLED if filled_quantity == quantity else None
    if filled_quantity == quantity and filled_quantity > 0:
        return BrokerFillState.FILLED
    if filled_quantity > 0:
        return BrokerFillState.PARTIALLY_FILLED
    return BrokerFillState.BROKER_REJECTED
