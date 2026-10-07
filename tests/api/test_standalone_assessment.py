from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.routes.agent import AgentKeyContext, require_agent_key
from packages.configuration.settings import Settings


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, model, key):
        return SimpleNamespace(assessment_policy={})

    async def scalar(self, statement):
        raise AssertionError("Standalone assessment must never read scanner records")


def client():
    app = create_app(Settings(infrastructure_checks=False))
    app.state.database = SimpleNamespace(sessions=Session)
    app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(
        workspace_id=uuid4(), key_id=uuid4()
    )
    return TestClient(app)


def trade():
    from datetime import UTC, datetime, timedelta

    expiry = (datetime.now(UTC) + timedelta(days=30)).strftime("%y%m%d")
    return {
        "legs": [{"symbol": f"AAPL{expiry}C00200000", "side": "buy"}],
        "quantity": 1,
        "limit_price": "2",
    }


def test_standalone_endpoint_needs_no_watchlist_or_client_broker_account():
    response = client().post("/api/v2/option-trade-assessments", json=trade())
    assert response.status_code == 200
    result = response.json()
    assert result["decision"] == "UNAVAILABLE"
    assert result["market_data"]["source"] == "unknown"
    assert result["market_data"]["feed"] is None
    assert result["market_data"]["requested_feed"] == "indicative"
    assert result["market_data"]["quality"] == "testing_only"
    assert result["market_data"]["greeks_calculation_freshness"] == "unknown"
    assert result["signal_score"] is None
    assert result["score_profile"] == "catalyst_momentum_v1"
    assert result["score_components"] is None
    assert result["evidence_as_of"] is None
    assert result["blocking_reasons"] == ["market_data_unavailable"]
    assert result["execution_allowed"] is False
    assert result["remediation"]


def test_on_demand_evidence_passes_for_never_scanned_symbol():
    from datetime import UTC, datetime
    from decimal import Decimal

    from packages.domain.options import Greeks, OptionContract, OptionQuote
    from packages.domain.workflow import CatalystFeatures

    now = datetime.now(UTC)

    class EvidenceProvider:
        async def get_evidence(self, proposal):
            return SimpleNamespace(
                contracts=(
                    OptionContract(
                        contract_id="fixture-contract",
                        symbol=proposal.legs[0].symbol,
                        underlying_symbol="AAPL",
                        expiration=datetime.strptime(
                            proposal.legs[0].symbol[-15:-9], "%y%m%d"
                        ).date(),
                        strike=Decimal(200),
                        option_type="call",
                        multiplier=100,
                        tradable=True,
                        quote=OptionQuote(
                            bid="1.95",
                            ask="2.05",
                            bid_size=10,
                            ask_size=10,
                            quoted_at=now,
                            open_interest=1000,
                            greeks=Greeks(delta=".5", gamma=".01", theta="-.01", vega=".1"),
                        ),
                    ),
                ),
                features=CatalystFeatures(
                    catalyst_confidence=".9",
                    sentiment=".8",
                    relative_volume=3,
                    price_momentum=".8",
                    gap_percent=1,
                    market_confirmation=".8",
                    sector_confirmation=".8",
                    liquidity_score=".9",
                ),
                features_at=now,
                spot=Decimal(200),
                greeks_at=now,
                greeks_source="synthetic-test-provider",
            )

    c = client()
    c.app.state.trade_assessment_provider_factory = lambda key: EvidenceProvider()
    c.app.state.database.sessions = lambda: Session()
    response = c.post("/api/v2/option-trade-assessments", json=trade())
    assert response.status_code == 200
    result = response.json()
    assert result["decision"] == "PASS", result
    assert result["signal_score"] >= float(result["minimum_passing_score"])
    assert result["execution_allowed"] is False
    assert result["blocking_reasons"] == []
    assert result["trade_fingerprint"]
    assert result["expires_at"]
    assert result["score_profile"] == "catalyst_momentum_v1"
    assert result["score_components"]
    assert result["evidence_as_of"] == now.isoformat()
    assert result["market_data"]["feed"] == "indicative"
    assert result["market_data"]["quality"] == "testing_only"
    assert result["market_data"]["source_quote_times"] == {
        trade()["legs"][0]["symbol"]: now.isoformat()
    }
    from packages.connected.trade_assessment import TradeRequest, validate_pass_receipt

    assert validate_pass_receipt(result, TradeRequest.model_validate(trade()))


def test_real_provider_factory_uses_canonical_saved_alpaca_credentials(monkeypatch):
    import apps.api.routes.assessment as route
    from packages.connected.direct_evidence import EvidenceUnavailable

    workspace_id = uuid4()
    c = client()
    c.app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(
        workspace_id=workspace_id, key_id=uuid4()
    )
    record = SimpleNamespace(
        enabled=True,
        validation_status="VERIFIED",
        key_version=7,
        nonce=b"synthetic-nonce",
        ciphertext=b"synthetic-ciphertext",
        fingerprint="synthetic-fingerprint",
    )
    requested = []

    class Store:
        def __init__(self, sessions, cipher):
            assert cipher is c.app.state.credential_cipher

        async def get(self, actual_workspace_id, provider):
            requested.append((actual_workspace_id, provider))
            return record

    class Cipher:
        def decrypt(self, actual_workspace_id, provider, version, nonce, ciphertext):
            assert (actual_workspace_id, provider, version) == (workspace_id, "ALPACA_PAPER", 7)
            assert nonce == b"synthetic-nonce"
            assert ciphertext == b"synthetic-ciphertext"
            return {"api_key_id": "SYNTHETIC_KEY_ID", "secret_key": "SYNTHETIC_SECRET"}

    received = []

    class Provider:
        def __init__(self, api_key_id, secret_key):
            received.append((api_key_id, secret_key))

        async def get_evidence(self, proposal):
            raise EvidenceUnavailable("synthetic_evidence_reached")

    monkeypatch.setattr(route, "CredentialStore", Store)
    monkeypatch.setattr(route, "DirectEvidenceProvider", Provider)
    c.app.state.credential_cipher = Cipher()
    response = c.post("/api/v2/option-trade-assessments", json=trade())
    assert requested == [(workspace_id, "ALPACA_PAPER")]
    assert received == [("SYNTHETIC_KEY_ID", "SYNTHETIC_SECRET")]
    assert response.status_code == 200
    assert response.json()["blocking_reasons"] == ["synthetic_evidence_reached"]


def test_real_provider_factory_sanitizes_missing_or_malformed_credentials(monkeypatch):
    import apps.api.routes.assessment as route

    c = client()
    record = SimpleNamespace(
        enabled=True,
        validation_status="VERIFIED",
        key_version=7,
        nonce=b"synthetic-nonce",
        ciphertext=b"synthetic-ciphertext",
        fingerprint="synthetic-fingerprint",
    )

    class Store:
        def __init__(self, sessions, cipher):
            pass

        async def get(self, workspace_id, provider):
            return record

    class Cipher:
        def decrypt(self, *args):
            return {"api_key": "SYNTHETIC_WRONG_NAME", "secret_key": "SYNTHETIC_SECRET"}

    def unexpected_provider(*args):
        raise AssertionError("Malformed stored credentials must not construct a provider")

    monkeypatch.setattr(route, "CredentialStore", Store)
    monkeypatch.setattr(route, "DirectEvidenceProvider", unexpected_provider)
    c.app.state.credential_cipher = Cipher()
    response = c.post("/api/v2/option-trade-assessments", json=trade())
    assert response.status_code == 200
    assert response.json()["decision"] == "UNAVAILABLE"
    assert response.json()["blocking_reasons"] == ["market_data_not_configured"]
    assert "SYNTHETIC" not in response.text


def test_auth_is_required_without_a_key():
    c = client()
    c.app.dependency_overrides.clear()
    assert c.post("/api/v2/option-trade-assessments", json=trade()).status_code == 401


def test_policy_is_machine_readable_and_forbids_execution_authority():
    response = client().get("/api/v2/option-trade-assessments/policy")
    assert response.status_code == 200
    result = response.json()
    assert result["minimum_passing_score"]
    assert result["execution_allowed"] is False
    assert result["supported_strategies"]


def test_provider_failure_returns_specific_unavailable_feedback():
    from packages.connected.direct_evidence import EvidenceUnavailable

    class Provider:
        async def get_evidence(self, trade):
            raise EvidenceUnavailable("market_data_permission_denied")

    c = client()
    c.app.state.trade_assessment_provider_factory = lambda key: Provider()
    result = c.post("/api/v2/option-trade-assessments", json=trade()).json()
    assert result["decision"] == "UNAVAILABLE"
    assert result["blocking_reasons"] == ["market_data_permission_denied"]


def test_caller_evidence_is_rejected_at_the_public_boundary():
    body = {**trade(), "signal_score": 100, "market_evidence_at": "2026-10-06T15:00:00Z"}
    assert client().post("/api/v2/option-trade-assessments", json=body).status_code == 422


def test_read_only_assessment_deadline_returns_unavailable_without_leaking_details(monkeypatch):
    import apps.api.routes.assessment as route

    async def expired(awaitable, **_kwargs):
        awaitable.close()
        raise TimeoutError("fixture-private-detail")

    monkeypatch.setattr(route.asyncio, "wait_for", expired)
    c = client()
    c.app.state.trade_assessment_provider_factory = lambda key: object()
    response = c.post("/api/v2/option-trade-assessments", json=trade())
    assert response.status_code == 200
    result = response.json()
    assert result["decision"] == "UNAVAILABLE"
    assert result["blocking_reasons"] == ["assessment_deadline_exceeded"]
    assert result["market_data"]["source"] == "unknown"
    assert "fixture-private-detail" not in response.text
