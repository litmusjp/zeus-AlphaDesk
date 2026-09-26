from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from apps.api.routes.desk import ExitPlanInput
from packages.connected.market_clock import ConnectedMarketClock
from packages.connected.opportunities import ConnectedAnalysis
from packages.database.models import ConditionalApprovalRecord
from packages.domain.broker import BrokerOrder, BrokerOrderLeg
from packages.domain.system import TradingEnvironment
from packages.domain.workflow import IntentLeg, OrderIntent, stable_client_order_id
from packages.execution import conditional_runner
from packages.execution.conditional_approval import (
    ApprovalState,
    ConditionalApproval,
    ConditionalExitApproval,
    ExitOrderSide,
    ExitPlanDecision,
    RevalidationDecision,
    approval_can_be_renewed,
    approval_is_active,
    evaluate_exit_plan,
    exit_plan_renewal_required_reason,
    revalidate_exit_for_submission,
    revalidate_for_submission,
    validate_exit_plan,
    validate_exit_plan_for_session,
)
from packages.execution.conditional_runner import _open_fill_response_is_valid, _open_order_matches
from packages.execution.conditional_store import (
    _broker_evidence_is_valid,
    _finish_transition_allowed,
)


def approval(**overrides: object) -> ConditionalApproval:
    values = {
        "approval_id": uuid4(),
        "workspace_id": uuid4(),
        "opportunity_id": uuid4(),
        "client_order_id": "ad-test-order",
        "session_date": date(2026, 9, 18),
        "approved_at": datetime(2026, 9, 17, 12, tzinfo=UTC),
        "expires_at": datetime(2026, 9, 19, tzinfo=UTC),
        "structure_fingerprint": "AAPL-20261016-200C-205C",
        "max_limit_price": Decimal("2.20"),
        "max_loss": Decimal("220"),
        "max_quantity": 1,
        "max_quote_age_seconds": 30,
        "state": ApprovalState.APPROVED_FOR_SESSION,
    }
    values.update(overrides)
    return ConditionalApproval(**values)


def test_open_order_match_accepts_the_bound_live_environment() -> None:
    risk_decision_id = uuid4()
    legs = (IntentLeg(symbol="AAPL", side="buy", ratio=1),)
    intent = OrderIntent(
        order_intent_id=uuid4(),
        client_order_id=stable_client_order_id(
            risk_decision_id, legs, 1, Decimal("2.08"), "day", "LIMIT"
        ),
        risk_decision_id=risk_decision_id,
        legs=legs,
        quantity=1,
        limit_price=Decimal("2.08"),
        execution_policy="LIMIT",
        created_at=datetime(2026, 9, 22, 14, tzinfo=UTC),
    )
    order = BrokerOrder(
        broker_order_id="broker-1",
        client_order_id=intent.client_order_id,
        status="new",
        asset_class="us_option",
        order_type="limit",
        order_class="mleg",
        time_in_force="day",
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        limit_price=Decimal("2.08"),
        created_at=datetime(2026, 9, 22, 14, tzinfo=UTC),
        broker_account_id="live-account",
        environment=TradingEnvironment.LIVE.value,
        legs=(
            BrokerOrderLeg(
                broker_order_id="broker-1",
                symbol="AAPL",
                side="buy",
                quantity=Decimal("1"),
                filled_quantity=Decimal("0"),
                status="new",
            ),
        ),
    )

    assert _open_order_matches(
        order,
        intent,
        approved_client_order_id=intent.client_order_id,
        expected_broker_account_id="live-account",
        expected_environment=TradingEnvironment.LIVE,
    )


def test_revalidation_allows_one_approved_session_submission() -> None:
    result = revalidate_for_submission(
        approval(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 18),
        structure_fingerprint="AAPL-20261016-200C-205C",
        limit_price=Decimal("2.08"),
        maximum_loss=Decimal("208"),
        quantity=1,
        quote_age_seconds=4,
    )

    assert result.decision is RevalidationDecision.READY_TO_SUBMIT


def test_next_session_open_approval_requires_an_explicit_exit_plan() -> None:
    assert validate_exit_plan(None) is not None


def valid_exit_plan(**overrides: object) -> dict[str, object]:
    plan: dict[str, object] = {
        "opening_approval_id": "opening-1",
        "structure_fingerprint": "AAPL-20261016-200C-205C",
        "broker_account_id": "paper-account-1",
        "environment": "PAPER",
        "stop_loss": "220",
        "profit_target": "100",
        "expires_at": "2026-09-18T19:30:00+00:00",
        "stale_data_behavior": "FAIL_CLOSED",
    }
    plan.update(overrides)
    return plan


@pytest.mark.parametrize(
    ("pl", "reason"),
    [(Decimal("-220"), "exit_plan_stop_loss"), (Decimal("100"), "exit_plan_profit_target")],
)
def test_exit_plan_evaluates_protection_and_planned_profit_taking(pl: Decimal, reason: str) -> None:
    result = evaluate_exit_plan(
        valid_exit_plan(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        structure_fingerprint="AAPL-20261016-200C-205C",
        broker_account_id="paper-account-1",
        environment="PAPER",
        unrealized_pl=pl,
        quote_age_seconds=4,
        max_quote_age_seconds=30,
    )
    assert result.decision is ExitPlanDecision.CLOSE
    assert result.reason == reason


def test_exit_plan_fails_closed_for_stale_evidence_and_binding_mismatch() -> None:
    stale = evaluate_exit_plan(
        valid_exit_plan(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        structure_fingerprint="AAPL-20261016-200C-205C",
        broker_account_id="paper-account-1",
        environment="PAPER",
        unrealized_pl=Decimal("-1000"),
        quote_age_seconds=31,
        max_quote_age_seconds=30,
    )
    wrong_binding = evaluate_exit_plan(
        valid_exit_plan(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        structure_fingerprint="changed",
        broker_account_id="paper-account-1",
        environment="PAPER",
        unrealized_pl=Decimal("-1000"),
        quote_age_seconds=4,
        max_quote_age_seconds=30,
    )
    assert stale.decision is ExitPlanDecision.FAIL_CLOSED
    assert stale.reason == "stale_exit_evidence"
    assert wrong_binding.decision is ExitPlanDecision.FAIL_CLOSED
    assert wrong_binding.reason == "exit_plan_binding_mismatch"


def test_exit_plan_evaluates_time_expiry() -> None:
    result = evaluate_exit_plan(
        valid_exit_plan(),
        now=datetime(2026, 9, 19, 20, tzinfo=UTC),
        structure_fingerprint="AAPL-20261016-200C-205C",
        broker_account_id="paper-account-1",
        environment="PAPER",
        unrealized_pl=Decimal("10"),
        quote_age_seconds=4,
        max_quote_age_seconds=30,
    )
    assert result.decision is ExitPlanDecision.CLOSE
    assert result.reason == "exit_plan_expired"


def test_revalidation_rejects_price_outside_approved_bound() -> None:
    result = revalidate_for_submission(
        approval(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 18),
        structure_fingerprint="AAPL-20261016-200C-205C",
        limit_price=Decimal("2.21"),
        maximum_loss=Decimal("221"),
        quantity=1,
        quote_age_seconds=4,
    )

    assert result.decision is RevalidationDecision.CONDITION_FAILED
    assert result.reason == "limit_price_exceeds_approval"


def test_revalidation_rejects_stale_quote_and_wrong_structure() -> None:
    result = revalidate_for_submission(
        approval(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 18),
        structure_fingerprint="AAPL-20261016-200C-210C",
        limit_price=Decimal("2.08"),
        maximum_loss=Decimal("208"),
        quantity=1,
        quote_age_seconds=31,
    )

    assert result.decision is RevalidationDecision.CONDITION_FAILED
    assert result.reason in {"structure_changed", "quote_stale"}


def test_revalidation_allows_next_broker_session_after_holiday_or_early_close() -> None:
    result = revalidate_for_submission(
        approval(expires_at=datetime(2026, 9, 22, 20, tzinfo=UTC)),
        # The approval was created for the prior local date.  The broker clock
        # has already advanced to the next valid regular session (for example
        # after a holiday or an early close), so local-date equality is not an
        # execution authorization.
        now=datetime(2026, 9, 21, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 21),
        structure_fingerprint="AAPL-20261016-200C-205C",
        limit_price=Decimal("2.08"),
        maximum_loss=Decimal("208"),
        quantity=1,
        quote_age_seconds=4,
    )

    assert result.decision is RevalidationDecision.READY_TO_SUBMIT


def test_expired_approval_is_not_active_and_can_be_renewed() -> None:
    now = datetime(2026, 9, 18, 14, 31, tzinfo=UTC)

    assert not approval_is_active(
        ApprovalState.APPROVED_FOR_SESSION,
        datetime(2026, 9, 17, 20, 5, tzinfo=UTC),
        now,
    )
    assert approval_can_be_renewed(ApprovalState.EXPIRED)


def test_revalidating_approval_remains_active_and_cannot_be_renewed() -> None:
    now = datetime(2026, 9, 18, 14, 31, tzinfo=UTC)

    assert approval_is_active(ApprovalState.REVALIDATING, now - date.resolution, now)
    assert not approval_can_be_renewed(ApprovalState.REVALIDATING)


@pytest.mark.parametrize(
    "state",
    [
        ApprovalState.READY_TO_SUBMIT,
        ApprovalState.SUBMITTED,
        ApprovalState.PARTIALLY_FILLED,
        ApprovalState.FILLED,
        ApprovalState.SUBMISSION_UNCERTAIN,
    ],
)
def test_execution_states_cannot_be_renewed(state: ApprovalState) -> None:
    assert not approval_can_be_renewed(state)


def test_terminal_state_cannot_be_downgraded_by_stale_worker() -> None:
    assert not _finish_transition_allowed(ApprovalState.FILLED, ApprovalState.SUBMITTED)
    assert _finish_transition_allowed(
        ApprovalState.DISPATCH_AUTHORIZED, ApprovalState.SUBMISSION_UNCERTAIN
    )
    assert not _finish_transition_allowed(
        ApprovalState.DISPATCH_AUTHORIZED, ApprovalState.CONDITION_FAILED
    )
    assert not _finish_transition_allowed(
        ApprovalState.SUBMISSION_UNCERTAIN, ApprovalState.CONDITION_FAILED
    )
    assert _finish_transition_allowed(ApprovalState.SUBMISSION_UNCERTAIN, ApprovalState.FILLED)


def exit_approval(**overrides: object) -> ConditionalExitApproval:
    values = {
        "approval_id": uuid4(),
        "workspace_id": uuid4(),
        "position_asset_id": "asset-qqq-1",
        "position_symbol": "QQQ260918C00500000",
        "position_side": "long",
        "approved_quantity": Decimal("1"),
        "order_side": ExitOrderSide.SELL,
        "session_date": date(2026, 9, 18),
        "approved_at": datetime(2026, 9, 17, 12, tzinfo=UTC),
        "expires_at": datetime(2026, 9, 19, tzinfo=UTC),
        "client_order_id": "ad-exit-test-order",
        "limit_price_bound": Decimal("4.50"),
        "max_quote_age_seconds": 30,
        "state": ApprovalState.APPROVED_FOR_SESSION,
    }
    values.update(overrides)
    return ConditionalExitApproval(**values)


def test_exit_revalidation_allows_long_position_sell_at_or_above_floor() -> None:
    result = revalidate_exit_for_submission(
        exit_approval(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 18),
        position_asset_id="asset-qqq-1",
        position_symbol="QQQ260918C00500000",
        position_side="long",
        current_quantity=Decimal("1"),
        order_side=ExitOrderSide.SELL,
        limit_price=Decimal("4.55"),
        quote_age_seconds=4,
    )

    assert result.decision is RevalidationDecision.READY_TO_SUBMIT


def test_exit_revalidation_rejects_position_change_and_wrong_sell_bound() -> None:
    changed_position = revalidate_exit_for_submission(
        exit_approval(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 18),
        position_asset_id="asset-qqq-1",
        position_symbol="QQQ260918C00500000",
        position_side="long",
        current_quantity=Decimal("2"),
        order_side=ExitOrderSide.SELL,
        limit_price=Decimal("4.55"),
        quote_age_seconds=4,
    )
    below_floor = revalidate_exit_for_submission(
        exit_approval(),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 18),
        position_asset_id="asset-qqq-1",
        position_symbol="QQQ260918C00500000",
        position_side="long",
        current_quantity=Decimal("1"),
        order_side=ExitOrderSide.SELL,
        limit_price=Decimal("4.49"),
        quote_age_seconds=4,
    )

    assert changed_position.reason == "position_changed"
    assert below_floor.reason == "exit_limit_below_floor"


def test_exit_revalidation_allows_short_position_buy_at_or_below_ceiling() -> None:
    result = revalidate_exit_for_submission(
        exit_approval(
            position_side="short",
            order_side=ExitOrderSide.BUY,
            limit_price_bound=Decimal("5.00"),
        ),
        now=datetime(2026, 9, 18, 14, 31, tzinfo=UTC),
        session_date=date(2026, 9, 18),
        position_asset_id="asset-qqq-1",
        position_symbol="QQQ260918C00500000",
        position_side="short",
        current_quantity=Decimal("1"),
        order_side=ExitOrderSide.BUY,
        limit_price=Decimal("4.95"),
        quote_age_seconds=4,
    )

    assert result.decision is RevalidationDecision.READY_TO_SUBMIT


def broker_record(**overrides: object) -> ConditionalApprovalRecord:
    values: dict[str, object] = {
        "approval_id": uuid4(),
        "workspace_id": uuid4(),
        "opportunity_id": uuid4(),
        "approved_by_user_id": uuid4(),
        "state": ApprovalState.REVALIDATING,
        "approval_kind": "OPEN",
        "session_date": date(2026, 9, 18),
        "approved_at": datetime(2026, 9, 18, 12, tzinfo=UTC),
        "expires_at": datetime(2026, 9, 19, tzinfo=UTC),
        "client_order_id": "ad-test-order",
        "structure_fingerprint": "QQQ260918C00500000:buy:1",
        "approved_intent_payload": {"quantity": 1, "limit_price": "4.50"},
        "approved_structure_identity": {"legs": []},
        "approved_broker_account_id": "paper-account-1",
        "execution_environment": TradingEnvironment.PAPER.value,
        "max_limit_price": Decimal("4.50"),
        "max_loss": Decimal("450"),
        "max_quantity": 1,
        "max_quote_age_seconds": 30,
        "min_limit_price": None,
        "position_asset_id": None,
        "position_symbol": None,
        "position_side": None,
        "exit_order_side": None,
        "broker_order_id": None,
        "claimed_at": datetime(2026, 9, 18, 12, tzinfo=UTC),
        "claim_token": uuid4(),
        "submitted_at": None,
        "failure_reason": None,
        "created_at": datetime(2026, 9, 18, 12, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 18, 12, tzinfo=UTC),
    }
    values.update(overrides)
    return ConditionalApprovalRecord(**values)


@pytest.mark.parametrize("clock_status", ["closed", "unavailable", "error"])
async def test_closed_or_unavailable_clock_defers_before_execution_analysis(
    monkeypatch: pytest.MonkeyPatch, clock_status: str
) -> None:
    now = datetime.now(UTC)
    risk_id = uuid4()
    legs = (IntentLeg(symbol="AAPL261016C00200000", side="buy", ratio=1),)
    intent = OrderIntent(
        order_intent_id=uuid4(),
        client_order_id=stable_client_order_id(risk_id, legs, 1, Decimal("2.08"), "day", "LIMIT"),
        risk_decision_id=risk_id,
        legs=legs,
        quantity=1,
        limit_price=Decimal("2.08"),
        execution_policy="LIMIT",
        created_at=now,
    )
    record = broker_record(
        approved_intent_payload=intent.model_dump(mode="json"),
        client_order_id=intent.client_order_id,
        expires_at=now + timedelta(days=1),
        exit_plan_payload=valid_exit_plan(),
    )
    original = ConnectedAnalysis(
        opportunity_id=record.opportunity_id,
        symbol="AAPL",
        disposition="TRADE",
        observed_at=now,
        expires_at=now + timedelta(days=1),
        signal={},
    )
    session = AsyncMock()
    session.scalar.side_effect = [
        SimpleNamespace(payload=original.model_dump(mode="json")),
        SimpleNamespace(trading_environment="PAPER", assessment_policy={}),
    ]
    database = MagicMock()
    database.sessions.return_value.__aenter__.return_value = session
    store = AsyncMock()
    store.claim_next.side_effect = [record, None]
    monkeypatch.setattr(conditional_runner, "ConditionalApprovalStore", lambda _: store)
    monkeypatch.setattr(conditional_runner, "_recover_ready_to_submit", AsyncMock())
    credentials = MagicMock()
    credentials.return_value.reveal = AsyncMock(
        return_value={"api_key_id": "test-key", "secret_key": "test-secret"}
    )
    monkeypatch.setattr(conditional_runner, "CredentialStore", credentials)
    service = MagicMock()
    service.return_value.analyze = AsyncMock(
        return_value=original.model_copy(update={"disposition": "NO_TRADE"})
    )
    monkeypatch.setattr(conditional_runner, "ConnectedOpportunityService", service)
    clock = ConnectedMarketClock(
        is_open=False,
        timestamp=now,
        next_open=now + timedelta(days=1),
        next_close=now + timedelta(days=1, hours=6),
    )
    adapter = MagicMock()
    adapter.return_value.get_clock = AsyncMock(
        return_value=clock if clock_status == "closed" else None,
        side_effect=RuntimeError("clock unavailable") if clock_status == "error" else None,
    )
    monkeypatch.setattr(conditional_runner, "AlpacaMarketClockAdapter", adapter)
    execute = AsyncMock()
    monkeypatch.setattr(conditional_runner, "execute_connected_order", execute)

    await conditional_runner.process_workspace_approvals(
        database=database, cipher=MagicMock(), workspace_id=record.workspace_id, now=now
    )

    service.return_value.analyze.assert_not_awaited()
    adapter.assert_called_once_with("test-key", "test-secret", environment=TradingEnvironment.PAPER)
    adapter.return_value.get_clock.assert_awaited_once_with()
    store.release_revalidation.assert_awaited_once_with(
        record.approval_id,
        workspace_id=record.workspace_id,
        claim_token=record.claim_token,
        now=ANY,
        reason=(
            "market_session_closed"
            if clock_status == "closed"
            else "authoritative_market_clock_unavailable"
        ),
    )
    store.finish.assert_not_awaited()
    store.authorize_submission.assert_not_awaited()
    execute.assert_not_awaited()
    store.claim_next.assert_awaited_once()


def broker_order(**overrides: object) -> BrokerOrder:
    values: dict[str, object] = {
        "broker_order_id": "broker-1",
        "client_order_id": "ad-test-order",
        "status": "new",
        "asset_class": "us_option",
        "symbol": None,
        "side": None,
        "order_type": "limit",
        "order_class": "mleg",
        "time_in_force": "day",
        "quantity": Decimal("1"),
        "filled_quantity": Decimal("0"),
        "filled_average_price": None,
        "limit_price": Decimal("4.50"),
        "submitted_at": datetime(2026, 9, 18, 12, tzinfo=UTC),
        "created_at": datetime(2026, 9, 18, 12, tzinfo=UTC),
        "updated_at": None,
        "legs": (
            BrokerOrderLeg(
                broker_order_id="broker-1",
                symbol="QQQ260918C00500000",
                side="buy",
                quantity=Decimal("1"),
                filled_quantity=Decimal("0"),
                status="new",
            ),
        ),
        "broker_account_id": "paper-account-1",
        "environment": "PAPER",
    }
    values.update(overrides)
    return BrokerOrder(**values)


def test_store_evidence_rejects_nonfinite_limit_price() -> None:
    valid_order = broker_order()
    malformed_order = BrokerOrder.model_construct(
        **{**valid_order.__dict__, "limit_price": Decimal("Infinity")}
    )
    assert not _broker_evidence_is_valid(
        broker_record(),
        ApprovalState.SUBMITTED,
        malformed_order,
    )


def test_open_and_store_evidence_accept_distinct_parent_and_leg_broker_order_ids() -> None:
    order = broker_order(
        legs=(broker_order().legs[0].model_copy(update={"broker_order_id": "broker-other"}),),
    )

    assert _open_fill_response_is_valid(order)
    assert _broker_evidence_is_valid(broker_record(), ApprovalState.SUBMITTED, order)


def test_store_evidence_accepts_the_bound_live_environment() -> None:
    record = broker_record(
        execution_environment=TradingEnvironment.LIVE.value,
        approved_broker_account_id="live-account-1",
    )
    order = broker_order(
        broker_account_id="live-account-1",
        environment=TradingEnvironment.LIVE.value,
    )

    assert _broker_evidence_is_valid(record, ApprovalState.SUBMITTED, order)


@pytest.mark.parametrize("leg_broker_order_id", ["", None])
def test_open_and_store_evidence_reject_missing_or_empty_leg_broker_order_id(
    leg_broker_order_id: str | None,
) -> None:
    leg = broker_order().legs[0].model_copy(update={"broker_order_id": leg_broker_order_id})
    order = broker_order(legs=(leg,))

    assert not _open_fill_response_is_valid(order)
    assert not _broker_evidence_is_valid(broker_record(), ApprovalState.SUBMITTED, order)


def test_store_evidence_rejects_price_above_approval_bound() -> None:
    order = broker_order(limit_price=Decimal("4.51"))
    assert not _broker_evidence_is_valid(
        broker_record(),
        ApprovalState.SUBMITTED,
        order,
    )


def test_store_evidence_rejects_invalid_average_price_without_fill() -> None:
    valid_order = broker_order()
    malformed_order = BrokerOrder.model_construct(
        **{
            **valid_order.__dict__,
            "filled_quantity": Decimal("0"),
            "filled_average_price": Decimal("NaN"),
        }
    )
    assert not _broker_evidence_is_valid(
        broker_record(),
        ApprovalState.SUBMITTED,
        malformed_order,
    )


def test_store_evidence_preserves_a_full_terminal_fill() -> None:
    order = broker_order(
        status="canceled",
        filled_quantity=Decimal("1"),
        filled_average_price=Decimal("4.50"),
        legs=(
            BrokerOrderLeg(
                broker_order_id="broker-1",
                symbol="QQQ260918C00500000",
                side="buy",
                quantity=Decimal("1"),
                filled_quantity=Decimal("1"),
                status="canceled",
            ),
        ),
    )
    assert _broker_evidence_is_valid(broker_record(), ApprovalState.FILLED, order)


@pytest.mark.parametrize(
    ("target", "filled_quantity", "expected"),
    [
        (ApprovalState.SUBMITTED, Decimal("0"), True),
        (ApprovalState.PARTIALLY_FILLED, Decimal("0.5"), True),
        (ApprovalState.FILLED, Decimal("1"), True),
        (ApprovalState.SUBMITTED, Decimal("1"), False),
        (ApprovalState.PARTIALLY_FILLED, Decimal("0"), False),
    ],
)
def test_store_evidence_applies_done_for_day_fill_matrix(
    target: ApprovalState, filled_quantity: Decimal, expected: bool
) -> None:
    order = broker_order(
        status="done_for_day",
        filled_quantity=filled_quantity,
        filled_average_price=Decimal("4.50") if filled_quantity else None,
        legs=tuple(
            leg.model_copy(
                update={
                    "filled_quantity": filled_quantity,
                    "status": "filled" if filled_quantity == Decimal("1") else "done_for_day",
                }
            )
            for leg in broker_order().legs
        ),
    )
    assert _broker_evidence_is_valid(broker_record(), target, order) is expected


def test_exit_plan_rejects_naive_timestamp_at_api_boundary() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ExitPlanInput(
            stop_loss=Decimal("220"),
            expires_at=datetime(2026, 9, 26, 22),
            stale_data_behavior="FAIL_CLOSED",
        )


def test_japan_local_saturday_can_be_a_valid_us_exchange_instant() -> None:
    local_display = datetime(2026, 9, 26, 1, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert local_display.astimezone(ZoneInfo("America/New_York")).date() == date(2026, 9, 25)
    assert validate_exit_plan(valid_exit_plan(expires_at=local_display.isoformat())) is None


@pytest.mark.parametrize(
    ("expires_at", "session_date", "reason"),
    [
        (
            "2026-09-26T15:00:00+00:00",
            date(2026, 9, 26),
            "exit_plan_expiry_not_regular_session",
        ),
        (
            "2026-09-28T05:22:00+00:00",
            date(2026, 9, 28),
            "exit_plan_expiry_before_session_open",
        ),
    ],
)
def test_legacy_exit_plan_session_boundary_fails_closed(
    expires_at: str, session_date: date, reason: str
) -> None:
    invalid = validate_exit_plan_for_session(
        valid_exit_plan(expires_at=expires_at), session_date=session_date
    )

    assert invalid == reason
    assert exit_plan_renewal_required_reason(invalid) == (
        f"exit_plan_invalid_requires_renewed_approval:{reason}"
    )


def test_legacy_exit_plan_accepts_friday_new_york_when_displayed_as_saturday_in_japan() -> None:
    # 11:30 Friday in New York is 00:30 Saturday in Japan.
    local_display = datetime(2026, 9, 26, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo"))

    assert (
        validate_exit_plan_for_session(
            valid_exit_plan(expires_at=local_display.isoformat()),
            session_date=date(2026, 9, 25),
        )
        is None
    )


def test_persisted_exit_plan_is_not_mutated_and_invalid_plan_fails_closed() -> None:
    valid_plan = valid_exit_plan()
    valid_result = evaluate_exit_plan(
        valid_plan,
        now=datetime(2026, 9, 18, 12, tzinfo=UTC),
        structure_fingerprint="AAPL-20261016-200C-205C",
        broker_account_id="paper-account-1",
        environment="PAPER",
        unrealized_pl=Decimal("0"),
        quote_age_seconds=4,
        max_quote_age_seconds=30,
    )

    invalid_plan = valid_plan.copy()
    invalid_plan["expires_at"] = "2026-09-26T22:00:00"

    assert valid_result.decision is ExitPlanDecision.HOLD
    assert valid_plan == valid_exit_plan()
    assert validate_exit_plan(invalid_plan) == "exit_plan_expiry_not_timezone_aware"
    result = evaluate_exit_plan(
        invalid_plan,
        now=datetime(2026, 9, 26, 12, tzinfo=UTC),
        structure_fingerprint="AAPL-20261016-200C-205C",
        broker_account_id="paper-account-1",
        environment="PAPER",
        unrealized_pl=Decimal("-1000"),
        quote_age_seconds=4,
        max_quote_age_seconds=30,
    )

    assert result.decision is ExitPlanDecision.FAIL_CLOSED
    assert result.reason == "exit_plan_expiry_not_timezone_aware"
