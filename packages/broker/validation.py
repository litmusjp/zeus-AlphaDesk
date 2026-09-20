from __future__ import annotations

from decimal import Decimal

from packages.domain.broker import BrokerAccount, BrokerOrder, BrokerPosition
from packages.execution.order_state import broker_fill_state


def validate_broker_account(account: BrokerAccount) -> str | None:
    if account.environment.upper() != "PAPER":
        return "paper_environment_required"
    if not account.account_id:
        return "broker_account_identity_missing"
    return None


def validate_broker_position(position: BrokerPosition, account: BrokerAccount) -> str | None:
    if position.broker_account_id != account.account_id:
        return "broker_account_identity_mismatch"
    if (position.environment or "").upper() != "PAPER":
        return "paper_environment_required"
    if position.asset_class.lower() not in {"us_option", "us_equity"}:
        return "unknown_asset_class"
    if not position.quantity.is_finite() or position.quantity <= 0:
        return "position_quantity_invalid"
    if position.quantity_available is not None and (
        not position.quantity_available.is_finite()
        or position.quantity_available < 0
        or position.quantity_available > position.quantity
    ):
        return "position_quantity_invalid"
    return None


def validate_broker_order_leg_identity(order: BrokerOrder) -> str | None:
    if not order.broker_order_id:
        return "broker_order_identity_missing"
    for leg in order.legs:
        if not leg.broker_order_id:
            return "order_leg_identity_missing"
    return None


def validate_broker_order(order: BrokerOrder, account: BrokerAccount) -> str | None:
    if order.broker_account_id != account.account_id:
        return "broker_account_identity_mismatch"
    if (order.environment or "").upper() != "PAPER":
        return "paper_environment_required"
    if not order.broker_order_id or not order.client_order_id:
        return "broker_order_identity_missing"
    if order.asset_class.lower() not in {"us_option", "us_equity"}:
        return "unknown_asset_class"
    if order.quantity is None or not order.quantity.is_finite() or order.quantity <= 0:
        return "order_quantity_invalid"
    if not order.filled_quantity.is_finite() or not 0 <= order.filled_quantity <= order.quantity:
        return "order_fill_invalid"
    if order.filled_average_price is not None and (
        not order.filled_average_price.is_finite() or order.filled_average_price <= 0
    ):
        return "order_fill_invalid"
    if order.filled_quantity > 0 and order.filled_average_price is None:
        return "order_fill_invalid"
    if order.order_type.lower() != "limit" or order.time_in_force.lower() != "day":
        return "order_terms_invalid"
    if order.limit_price is None or not order.limit_price.is_finite() or order.limit_price <= 0:
        return "order_price_invalid"
    if broker_fill_state(order.status, order.filled_quantity, order.quantity) is None:
        return "order_status_fill_contradictory"
    identity_error = validate_broker_order_leg_identity(order)
    if identity_error is not None:
        return identity_error

    order_class = order.order_class.lower()
    if order_class == "simple":
        if not order.symbol or order.side not in {"buy", "sell"} or order.legs:
            return "simple_order_shape_invalid"
        return None
    if order_class != "mleg" or not order.legs:
        return "order_legs_invalid"

    for leg in order.legs:
        if not leg.symbol or leg.side not in {"buy", "sell"}:
            return "order_leg_shape_invalid"
        if leg.quantity is None or not leg.quantity.is_finite() or leg.quantity <= 0:
            return "order_leg_quantity_invalid"
        if not leg.filled_quantity.is_finite() or not 0 <= leg.filled_quantity <= leg.quantity:
            return "order_leg_fill_invalid"
        if broker_fill_state(leg.status, leg.filled_quantity, leg.quantity) is None:
            return "order_leg_status_fill_contradictory"

    if order.filled_quantity == 0 and any(leg.filled_quantity != 0 for leg in order.legs):
        return "order_leg_fill_mismatch"
    if 0 < order.filled_quantity < order.quantity and any(
        leg.filled_quantity
        != (leg.quantity or Decimal("0")) * order.filled_quantity / order.quantity
        for leg in order.legs
    ):
        return "order_leg_fill_mismatch"
    if order.filled_quantity == order.quantity and any(
        leg.filled_quantity != leg.quantity for leg in order.legs
    ):
        return "order_leg_fill_mismatch"
    return None
