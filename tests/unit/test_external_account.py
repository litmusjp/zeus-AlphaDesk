from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr

from apps.api.app import create_app
from packages.configuration.settings import Settings
from packages.connected.external_account import (
    AlpacaExternalAccountProvider,
    ExternalAccountBinding,
    _NoRedirectHandler,
)
from packages.domain.workflow import CatalystFeatures


@pytest.mark.asyncio
async def test_external_provider_binds_key_sandbox_account_and_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    key_l1 = uuid4()
    key_l2 = uuid4()
    provider = AlpacaExternalAccountProvider(
        (
            ExternalAccountBinding(workspace_id, key_l1, "L1", "sandbox-1", "PAPER", "token-1"),
            ExternalAccountBinding(workspace_id, key_l2, "L2", "sandbox-2", "PAPER", "token-2"),
        )
    )
    seen: list[str] = []

    def get(token: str) -> dict[str, object]:
        seen.append(token)
        return {
            "id": "L1",
            "status": "ACTIVE",
            "equity": "1000",
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        }

    monkeypatch.setattr(provider, "_get", get)
    assert await provider.get_account(
        workspace_id=workspace_id,
        key_id=key_l2,
        account_id="L1",
        sandbox_id="sandbox-1",
        environment="PAPER",
    ) is None
    assert await provider.get_account(
        workspace_id=workspace_id,
        key_id=key_l1,
        account_id="L1",
        sandbox_id="sandbox-1",
        environment="LIVE",
    ) is None
    evidence = await provider.get_account(
        workspace_id=workspace_id,
        key_id=key_l1,
        account_id="L1",
        sandbox_id="sandbox-1",
        environment="PAPER",
    )
    assert evidence is not None
    assert evidence.equity == Decimal("1000")
    assert seen == ["token-1"]


@pytest.mark.asyncio
async def test_external_provider_rejects_broker_failures_and_bad_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = ExternalAccountBinding(uuid4(), uuid4(), "L1", "sandbox-1", "PAPER", "token")
    provider = AlpacaExternalAccountProvider((binding,))

    monkeypatch.setattr(
        provider,
        "_get",
        lambda _token: {"id": "other", "status": "ACTIVE", "equity": "1000"},
    )
    assert await provider.get_account(
        workspace_id=binding.workspace_id,
        key_id=binding.key_id,
        account_id=binding.account_id,
        sandbox_id=binding.sandbox_id,
        environment=binding.environment,
    ) is None

    monkeypatch.setattr(provider, "_get", lambda _token: (_ for _ in ()).throw(OSError()))
    assert await provider.get_account(
        workspace_id=binding.workspace_id,
        key_id=binding.key_id,
        account_id=binding.account_id,
        sandbox_id=binding.sandbox_id,
        environment=binding.environment,
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"id": "L1", "status": "ACTIVE", "equity": "1000", "account_blocked": False,
         "trade_suspended_by_user": False},
        {"id": "L1", "status": "ACTIVE", "equity": "NaN", "trading_blocked": False,
         "account_blocked": False, "trade_suspended_by_user": False},
        {"id": "L1", "status": "ACTIVE", "equity": "1000", "trading_blocked": "false",
         "account_blocked": False, "trade_suspended_by_user": False},
    ],
)
async def test_external_provider_requires_strict_broker_evidence(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    binding = ExternalAccountBinding(uuid4(), uuid4(), "L1", "sandbox-1", "PAPER", "token")
    provider = AlpacaExternalAccountProvider((binding,))
    monkeypatch.setattr(provider, "_get", lambda _token: body)
    assert await provider.get_account(
        workspace_id=binding.workspace_id,
        key_id=binding.key_id,
        account_id=binding.account_id,
        sandbox_id=binding.sandbox_id,
        environment=binding.environment,
    ) is None


def test_external_binding_config_rejects_malformed_and_duplicate_keys() -> None:
    import json

    record = {
        "workspace_id": str(uuid4()),
        "key_id": str(uuid4()),
        "account_id": "L1",
        "sandbox_id": "sandbox-1",
        "environment": "PAPER",
        "access_token": "placeholder",
    }
    with pytest.raises(ValueError):
        AlpacaExternalAccountProvider.from_json("not-json")
    with pytest.raises(ValueError):
        AlpacaExternalAccountProvider.from_json(json.dumps({"bindings": [record, record]}))
    provider = AlpacaExternalAccountProvider.from_json(json.dumps({"bindings": [record]}))
    assert "placeholder" not in repr(provider)


def test_external_provider_rejects_redirects() -> None:
    with pytest.raises(OSError):
        _NoRedirectHandler().redirect_request(None)


@pytest.mark.asyncio
async def test_optional_binding_loader_wires_provider_and_malformed_config_fails_closed() -> None:
    import json

    record = {
        "workspace_id": str(uuid4()),
        "key_id": str(uuid4()),
        "account_id": "L1",
        "sandbox_id": "sandbox-1",
        "environment": "PAPER",
        "access_token": "placeholder",
    }
    app = create_app(
        Settings(
            infrastructure_checks=False,
            external_account_bindings_json=SecretStr(json.dumps({"bindings": [record]})),
        )
    )
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.external_account_provider, AlpacaExternalAccountProvider)
        assert app.state.readiness["external_account_bindings"] == "healthy"

    malformed = create_app(
        Settings(infrastructure_checks=False, external_account_bindings_json=SecretStr("{}"))
    )
    async with malformed.router.lifespan_context(malformed):
        assert malformed.state.external_account_provider is None
        assert malformed.state.readiness["external_account_bindings"] == "unhealthy"


def test_external_assessment_does_not_trust_request_features() -> None:
    from packages.connected.assessment_policy import AssessmentPolicy
    from packages.connected.strategy_assessment import assess_strategy
    from packages.domain.workflow import CatalystFeatures
    from tests.unit.test_strategy_assessment import payload

    features = CatalystFeatures(
        catalyst_confidence="1", sentiment="1", relative_volume="4", price_momentum="1",
                    gap_percent="0", market_confirmation="1", sector_confirmation="1",
                    liquidity_score="1",
    )
    result = assess_strategy(
        payload(
            market_scanner_features=features,
            external_account_id="L1",
            external_sandbox_id="sandbox-1",
            external_environment="PAPER",
        ),
        AssessmentPolicy(),
        paper_equity=Decimal("1000"),
        broker_evidence_available=True,
        now=datetime.now(UTC),
    )
    assert result.market_scanner_signal_score is None
    assert result.decision == "UNAVAILABLE"
    assert "market_scanner_score_evidence" in result.failed_check_codes


@pytest.mark.parametrize(
    ("features", "decision", "failed_code"),
    [
        (
            CatalystFeatures(
                catalyst_confidence="1", sentiment="1", relative_volume="4", price_momentum="1",
                gap_percent="0", market_confirmation="1", sector_confirmation="1",
                liquidity_score="1",
            ),
            "PASS",
            None,
        ),
        (
            CatalystFeatures(
                catalyst_confidence="0", sentiment="0", relative_volume="1", price_momentum="0",
                    gap_percent="0", market_confirmation="0", sector_confirmation="0",
                    liquidity_score="0",
            ),
            "FAIL",
            "minimum_signal_score",
        ),
    ],
)
def test_external_trusted_score_enforces_threshold(
    features: object, decision: str, failed_code: str | None
) -> None:
    from packages.connected.assessment_policy import AssessmentPolicy
    from packages.connected.strategy_assessment import assess_strategy
    from packages.domain.workflow import CatalystFeatures
    from tests.unit.test_strategy_assessment import payload

    request = payload(
        external_account_id="L1",
        external_sandbox_id="sandbox-1",
        external_environment="PAPER",
    )
    now = request.observed_at
    result = assess_strategy(
        request,
        AssessmentPolicy(),
        paper_equity=Decimal("10000"),
        broker_evidence_available=True,
        now=now,
        trusted_market_features=features if isinstance(features, CatalystFeatures) else None,
        trusted_score_source="alphadesk_connected_opportunity",
        trusted_score_observed_at=now,
    )
    assert result.decision == decision
    assert (failed_code in result.failed_check_codes) if failed_code else result.pass_
    assert result.score_source == "alphadesk_connected_opportunity"
    assert result.score_observed_at == now


def test_external_stale_trusted_score_is_unavailable() -> None:
    from packages.connected.assessment_policy import AssessmentPolicy
    from packages.connected.strategy_assessment import assess_strategy
    from packages.domain.workflow import CatalystFeatures
    from tests.unit.test_strategy_assessment import payload

    now = datetime.now(UTC)
    features = CatalystFeatures(
        catalyst_confidence="1", sentiment="1", relative_volume="4", price_momentum="1",
        gap_percent="0", market_confirmation="1", sector_confirmation="1", liquidity_score="1",
    )
    result = assess_strategy(
        payload(
            external_account_id="L1",
            external_sandbox_id="sandbox-1",
            external_environment="PAPER",
        ),
        AssessmentPolicy(),
        paper_equity=Decimal("1000"),
        broker_evidence_available=True,
        now=now,
        trusted_market_features=features,
        trusted_score_source="alphadesk_connected_opportunity",
        trusted_score_observed_at=now - timedelta(hours=1),
    )
    assert result.decision == "UNAVAILABLE"
