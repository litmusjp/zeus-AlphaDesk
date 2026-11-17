"""Information-only option economics for the standalone assessment receipt.

Nothing here is a check: the block never changes a decision, a score or a threshold. Every field
that cannot be computed from the evidence AlphaDesk already holds is the string "unavailable".
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

UNAVAILABLE = "unavailable"
_PLACES = Decimal("0.0001")
RANGE_PROXY_NOTE = (
    "Proxy, not an expected move: the required move divided by the 20-session average high-low "
    "range scaled by the square root of the weekdays to expiry. Market holidays are not removed."
)
SPREAD_COST_BASIS = (
    "Half of the bid-ask width (summed over the legs of a vertical) divided by the premium "
    "(the net debit for a vertical): the cost of one crossing."
)


def _text(value: Decimal) -> str:
    return str(value.quantize(_PLACES))


def weekdays_to_expiry(today: date, expiry: date) -> int:
    """Weekdays after `today` up to and including `expiry`. Holidays are not removed."""
    count, day = 0, today
    while day < expiry:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def _guard(fn: Any) -> str:
    try:
        return _text(fn())
    except (ArithmeticError, InvalidOperation, TypeError, ValueError, AttributeError, KeyError):
        return UNAVAILABLE


def option_economics(
    *,
    legs: list[Any],
    contracts: dict[str, Any],
    spot: Decimal,
    premium: Decimal,
    today: date,
    average_range_pct: Decimal | None,
) -> dict[str, Any]:
    """`legs`: the proposal legs; `premium`: per share (the net debit for a vertical)."""
    vertical = len(legs) == 2
    block: dict[str, Any] = {
        "information_only": True,
        "structure": "debit_vertical" if vertical else "single_long_option",
        "spread_cost_basis": SPREAD_COST_BASIS,
        "range_proxy_note": RANGE_PROXY_NOTE,
    }
    try:
        long_leg = next(leg for leg in legs if leg.side == "buy")
        long_contract = contracts[long_leg.symbol]
        is_call = long_contract.option_type == "call"
        strike: Decimal = long_contract.strike
        selected = [contracts[leg.symbol] for leg in legs]
    except (StopIteration, KeyError, AttributeError, TypeError):
        for name in (
            "expiry_break_even",
            "required_move_pct",
            "spread_cost_pct",
            "theta_pct_per_day",
            "days_to_expiry",
            "range_proxy",
        ):
            block[name] = UNAVAILABLE
        return block

    block["premium"] = _guard(lambda: premium)
    if vertical:
        short_leg = next((leg for leg in legs if leg.side == "sell"), None)
        if short_leg is None or short_leg.symbol not in contracts:
            block["width"] = block["max_profit"] = UNAVAILABLE
        else:
            width = abs(strike - contracts[short_leg.symbol].strike)
            block["width"] = _guard(lambda: width)
            block["max_profit"] = _guard(lambda: width - premium)

    def break_even() -> Decimal:
        if premium <= 0 or spot <= 0:
            raise ArithmeticError("premium and spot must be positive")
        return strike + premium if is_call else strike - premium

    def required_move() -> Decimal:
        target = break_even()
        move = (target - spot) / spot if is_call else (spot - target) / spot
        return max(Decimal(0), move) * 100

    def spread_cost() -> Decimal:
        if premium <= 0:
            raise ArithmeticError("premium must be positive")
        half_widths = sum(((c.quote.ask - c.quote.bid) / 2 for c in selected), Decimal(0))
        if any(c.quote.ask < c.quote.bid for c in selected):
            raise ArithmeticError("crossed quote")
        return half_widths / premium * 100

    def theta_pct() -> Decimal:
        if premium <= 0:
            raise ArithmeticError("premium must be positive")
        net = Decimal(0)
        for leg in legs:
            greeks = contracts[leg.symbol].quote.greeks
            if greeks is None or greeks.theta is None:
                raise ArithmeticError("theta missing")
            net += greeks.theta if leg.side == "buy" else -greeks.theta
        return net / premium * 100

    def proxy() -> Decimal:
        weekdays = weekdays_to_expiry(today, min(c.expiration for c in selected))
        if average_range_pct is None or average_range_pct <= 0 or weekdays <= 0:
            raise ArithmeticError("range or weekdays missing")
        return required_move() / (average_range_pct * Decimal(weekdays).sqrt())

    block["expiry_break_even"] = _guard(break_even)
    block["required_move_pct"] = _guard(required_move)
    block["spread_cost_pct"] = _guard(spread_cost)
    block["theta_pct_per_day"] = _guard(theta_pct)
    try:
        block["days_to_expiry"] = (min(c.expiration for c in selected) - today).days
    except (TypeError, ValueError, AttributeError):
        block["days_to_expiry"] = UNAVAILABLE
    block["range_proxy"] = _guard(proxy)
    return block


CREDIT_COST_BASIS = (
    "Half of the bid-ask width summed over both legs divided by the net credit: the cost of one "
    "crossing, as a percent of the credit received."
)


def _net_theta(legs: list[Any], contracts: dict[str, Any]) -> Decimal:
    net = Decimal(0)
    for leg in legs:
        greeks = contracts[leg.symbol].quote.greeks
        if greeks is None or greeks.theta is None:
            raise ArithmeticError("theta missing")
        net += greeks.theta if leg.side == "buy" else -greeks.theta
    return net


def credit_economics(
    *, legs: list[Any], contracts: dict[str, Any], credit: Decimal, today: date
) -> dict[str, Any]:
    """Information-only block for a bull put or bear call credit vertical (all per share).

    A credit spread can profit with no favourable move, so there is no "required move" and no
    range proxy; those fields are reported as not applicable rather than invented.
    """
    block: dict[str, Any] = {
        "information_only": True,
        "structure": "credit_vertical",
        "spread_cost_basis": CREDIT_COST_BASIS,
        "required_move_pct": "not_applicable",
        "range_proxy": "not_applicable",
        "credit": _guard(lambda: credit),
    }
    try:
        short = next(leg for leg in legs if leg.side == "sell")
        long = next(leg for leg in legs if leg.side == "buy")
        short_contract, long_contract = contracts[short.symbol], contracts[long.symbol]
        is_call = short_contract.option_type == "call"
        selected = [short_contract, long_contract]
    except (StopIteration, KeyError, AttributeError, TypeError):
        for name in ("width", "max_profit", "max_loss", "expiry_break_even", "spread_cost_pct",
                     "net_theta_per_day_per_share", "days_to_expiry"):
            block[name] = UNAVAILABLE
        return block
    width = abs(short_contract.strike - long_contract.strike)
    block["width"] = _guard(lambda: width)
    block["max_profit"] = _guard(lambda: credit)
    block["max_loss"] = _guard(lambda: width - credit)
    block["expiry_break_even"] = _guard(
        lambda: short_contract.strike + credit if is_call else short_contract.strike - credit
    )

    def cost() -> Decimal:
        if credit <= 0 or any(c.quote.ask < c.quote.bid for c in selected):
            raise ArithmeticError("credit must be positive and quotes uncrossed")
        return sum(((c.quote.ask - c.quote.bid) / 2 for c in selected), Decimal(0)) / credit * 100

    block["spread_cost_pct"] = _guard(cost)
    block["net_theta_per_day_per_share"] = _guard(lambda: _net_theta(legs, contracts))
    try:
        block["days_to_expiry"] = (min(c.expiration for c in selected) - today).days
    except (TypeError, ValueError, AttributeError):
        block["days_to_expiry"] = UNAVAILABLE
    return block


def protective_economics(
    *,
    legs: list[Any],
    contracts: dict[str, Any],
    premium: Decimal,
    shares: int,
    stock_entry: Decimal,
    spot: Decimal,
    today: date,
) -> dict[str, Any]:
    """Information-only block for a protective put / put spread against declared shares."""
    spread = len(legs) == 2
    block: dict[str, Any] = {
        "information_only": True,
        "structure": "protective_put_spread" if spread else "protective_put",
        "stock_context_basis": "declared by the caller; not verified against any broker account",
        "premium": _guard(lambda: premium),
    }
    try:
        long = next(leg for leg in legs if leg.side == "buy")
        long_contract = contracts[long.symbol]
        short = next((leg for leg in legs if leg.side == "sell"), None)
        selected = [contracts[leg.symbol] for leg in legs]
    except (StopIteration, KeyError, AttributeError, TypeError):
        for name in ("floor_strike", "cost_pct_of_stock_value", "expiry_break_even",
                     "net_theta_per_day_per_share", "days_to_expiry"):
            block[name] = UNAVAILABLE
        return block
    block["floor_strike"] = _guard(lambda: long_contract.strike)
    if short is not None:
        short_contract = contracts[short.symbol]
        block["short_put_strike"] = _guard(lambda: short_contract.strike)
        block["residual_risk_note"] = (
            "Below the short put strike the long put is offset by the short put, so downside "
            "is not floored there."
        )
    block["cost_pct_of_stock_value"] = _guard(lambda: premium * 100 / spot if spot > 0 else 1 / 0)
    block["expiry_break_even"] = _guard(lambda: stock_entry + premium)
    block["net_theta_per_day_per_share"] = _guard(lambda: _net_theta(legs, contracts))
    block["declared_shares"] = shares
    try:
        block["days_to_expiry"] = (min(c.expiration for c in selected) - today).days
    except (TypeError, ValueError, AttributeError):
        block["days_to_expiry"] = UNAVAILABLE
    return block
