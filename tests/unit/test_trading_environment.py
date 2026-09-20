from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from packages.broker.validation import validate_broker_account
from packages.domain.broker import BrokerAccount, ReconciliationSnapshot
from packages.domain.system import TradingEnvironment
from packages.trading.environment import (
    LIVE_CONFIRMATION_PHRASE,
    ModeTransitionRequest,
    TradingModeReadiness,
    validate_mode_transition,
)
from packages.trading.preparation import LivePreparationError, prepare_live_target

NOW = datetime.now(UTC)


def _live_account() -> BrokerAccount:
    return BrokerAccount(
        account_id="live",
        environment="LIVE",
        account_number="L1",
        status="ACTIVE",
        currency="USD",
        equity=100,
        cash=100,
        buying_power=100,
        last_equity=100,
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
    )


def ready(**changes: object) -> TradingModeReadiness:
    values: dict[str, object] = {
        "is_admin": True,
        "target_credentials_verified": True,
        "target_account_authenticated_at": NOW,
        "target_account_id": "acct-live",
        "reconciled_at": NOW,
        "stream_connected": True,
        "account_active": True,
        "account_unblocked": True,
        "guardian_halted": False,
        "active_or_unresolved_orders": 0,
        "active_or_unresolved_approvals": 0,
        "ambiguous_submissions": 0,
        "active_submission_leases": 0,
        "broker_confirmed_positions": 0,
        "target_account_matches": True,
        "legacy_identity": False,
        "preparation_state": "PREPARED",
        "prepared_at": NOW,
        "prepared_environment": "LIVE",
        "prepared_credential_fingerprint": "fp",
        "current_credential_fingerprint": "fp",
    }
    values.update(changes)
    return TradingModeReadiness(**values)


def request(**changes: object) -> ModeTransitionRequest:
    values: dict[str, object] = {
        "actor_user_id": UUID("00000000-0000-0000-0000-000000000001"),
        "is_admin": True,
        "old_environment": TradingEnvironment.PAPER,
        "new_environment": TradingEnvironment.LIVE,
        "confirmation": LIVE_CONFIRMATION_PHRASE,
    }
    values.update(changes)
    return ModeTransitionRequest(**values)


def test_live_transition_requires_every_readiness_gate() -> None:
    decision = validate_mode_transition(request(), ready())

    assert decision.allowed is True
    assert decision.reason == "ready"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"is_admin": False}, "admin_required"),
        ({"target_credentials_verified": False}, "target_credentials_not_verified"),
        ({"confirmation": "ENABLE LIVE"}, "live_confirmation_required"),
        ({"active_or_unresolved_orders": 1}, "unresolved_orders"),
        ({"active_or_unresolved_approvals": 1}, "unresolved_approvals"),
        ({"ambiguous_submissions": 1}, "ambiguous_submissions"),
        ({"active_submission_leases": 1}, "active_submission_leases"),
        ({"broker_confirmed_positions": 1}, "broker_positions_present"),
        ({"stream_connected": False, "preparation_state": "STALE"}, "live_preparation_missing"),
        ({"guardian_halted": True}, "guardian_halted"),
        ({"legacy_identity": True}, "legacy_identity"),
        ({"target_account_matches": False}, "target_account_mismatch"),
    ],
)
def test_live_transition_fails_closed(change: dict[str, object], reason: str) -> None:
    transition = request(
        confirmation=change.pop("confirmation")
    ) if "confirmation" in change else request()
    decision = validate_mode_transition(transition, ready(**change))

    assert decision.allowed is False
    assert decision.reason == reason


def test_reconciliation_must_be_fresh() -> None:
    decision = validate_mode_transition(
        request(), ready(reconciled_at=NOW - timedelta(minutes=2))
    )

    assert decision.reason == "reconciliation_stale"


def test_broker_identity_must_match_the_explicit_expected_environment() -> None:
    account = _live_account()

    assert (
        validate_broker_account(account, TradingEnvironment.PAPER)
        == "broker_environment_mismatch"
    )
    assert validate_broker_account(account, TradingEnvironment.LIVE) is None


@pytest.mark.asyncio
async def test_preparation_is_read_only_and_never_claims_stream_connection() -> None:
    class FakeBroker:
        async def reconcile(self) -> ReconciliationSnapshot:
            return ReconciliationSnapshot(account=_live_account(), positions=(), open_orders=())

        async def close(self) -> None:
            return None

    evidence = await prepare_live_target(FakeBroker(), credential_fingerprint="fp")

    assert evidence["state"] == "PREPARED"
    assert evidence["target_environment"] == "LIVE"
    assert evidence["stream_connected"] is False


@pytest.mark.asyncio
async def test_preparation_rejects_a_paper_account() -> None:
    class FakeBroker:
        async def reconcile(self) -> ReconciliationSnapshot:
            return ReconciliationSnapshot(
                account=_live_account().model_copy(update={"environment": "PAPER"}),
                positions=(),
                open_orders=(),
            )

        async def close(self) -> None:
            return None

    with pytest.raises(LivePreparationError, match="broker_environment_mismatch"):
        await prepare_live_target(FakeBroker(), credential_fingerprint="fp")
