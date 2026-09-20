from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.api.routes.agent import _broker_evidence_is_ready
from packages.domain.broker import BrokerAccount, BrokerSyncStatus
from packages.domain.system import BrokerState

NOW = datetime(2026, 9, 20, 14, tzinfo=UTC)


def _account(**overrides: object) -> BrokerAccount:
    values: dict[str, object] = {
        "account_id": "paper-1",
        "environment": "PAPER",
        "account_number": "PA123",
        "status": "ACTIVE",
        "currency": "USD",
        "equity": Decimal("10000"),
        "cash": Decimal("10000"),
        "buying_power": Decimal("10000"),
        "last_equity": Decimal("10000"),
        "trading_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
        "as_of": NOW,
    }
    values.update(overrides)
    return BrokerAccount(**values)


def _status(**overrides: object) -> BrokerSyncStatus:
    values: dict[str, object] = {
        "state": BrokerState.RECONCILED,
        "last_reconciled_at": NOW,
        "stream_connected": True,
    }
    values.update(overrides)
    return BrokerSyncStatus(**values)


def test_assessment_requires_live_trade_updates_and_unblocked_paper_account() -> None:
    assert not _broker_evidence_is_ready(
        _status(stream_connected=False), _account(), now=NOW, maximum_age=timedelta(seconds=120)
    )
    assert not _broker_evidence_is_ready(
        _status(), _account(trading_blocked=True), now=NOW, maximum_age=timedelta(seconds=120)
    )
    assert _broker_evidence_is_ready(
        _status(), _account(), now=NOW, maximum_age=timedelta(seconds=120)
    )
