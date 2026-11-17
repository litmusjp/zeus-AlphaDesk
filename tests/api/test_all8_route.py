"""Phase B: the REAL FastAPI route gates new structures before any provider access, the workspace
policy control (`PUT /api/v1/desk/assessment-policy`) enables and restores them, and the managed
quality settings are untouched by it.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import packages.connected.strategy_assessment as strategy_module
import packages.connected.trade_assessment as trade_module
from apps.api.app import create_app
from apps.api.routes.agent import AgentKeyContext, require_agent_key
from packages.auth.dependencies import WorkspaceContext, require_workspace
from packages.configuration.settings import Settings
from packages.connected.assessment_policy import AssessmentPolicy
from packages.database.models import ManagedReviewGateRecord, WorkspaceRecord
from tests.unit.test_all8_assessment import (
    BEAR_CALL,
    BEAR_CALL_CONTRACTS,
    BULL_PUT,
    BULL_PUT_CONTRACTS,
    C200,
    C205,
    HEDGE_PUT,
    NOW,
    P185,
    PROT_PUT,
    PROT_SPREAD,
    STOCK,
    evidence,
)

WORKSPACE, KEY = (
    UUID("11111111-1111-4111-8111-111111111111"),
    UUID("22222222-2222-4222-8222-222222222222"),
)
ALL_EIGHT = [
    "long_call",
    "long_put",
    "bull_call_debit_spread",
    "bear_put_debit_spread",
    "bull_put_credit_spread",
    "bear_call_credit_spread",
    "protective_put",
    "protective_put_spread",
]


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(trade_module, "datetime", Frozen)
    monkeypatch.setattr(strategy_module, "datetime", Frozen)


class Provider:
    def __init__(self):
        self.calls = 0
        self.by_symbols = {
            frozenset({C200.symbol, C205.symbol}): evidence((C200, C205)),
            frozenset({s for s, _ in BULL_PUT}): evidence(BULL_PUT_CONTRACTS),
            frozenset({s for s, _ in BEAR_CALL}): evidence(BEAR_CALL_CONTRACTS, bullish=False),
            frozenset({HEDGE_PUT.symbol}): evidence((HEDGE_PUT,)),
            frozenset({HEDGE_PUT.symbol, P185.symbol}): evidence((HEDGE_PUT, P185)),
        }

    async def get_evidence(self, trade):
        self.calls += 1
        return self.by_symbols[frozenset(leg.symbol for leg in trade.legs)]


class FakeDatabase:
    """Just enough of the session API for the route and the desk policy PUT/GET."""

    def __init__(self, stored: dict, gate=None):
        self.workspace = SimpleNamespace(assessment_policy=stored, updated_at=None)
        self.gate = gate
        self.audits = []
        outer = self

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, model, key, **kwargs):
                if model is WorkspaceRecord:
                    return outer.workspace
                if model is ManagedReviewGateRecord:
                    return outer.gate
                raise AssertionError(model)

            async def scalars(self, statement):
                return []

            def add(self, record):
                outer.audits.append(record)

        class Sessions:
            def __call__(self):
                return Session()

            def begin(self):
                return Session()

        self.sessions = Sessions()


def make_client(stored=None, gate=None):
    provider = Provider()
    database = FakeDatabase(dict(stored or {}), gate)
    app = create_app(Settings(infrastructure_checks=False))
    app.state.database = database
    app.state.trade_assessment_provider_factory = lambda key: provider
    app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(
        workspace_id=WORKSPACE, key_id=KEY
    )
    principal = SimpleNamespace(user_id=UUID(int=7), auth_subject="s", email="e", is_admin=False)
    app.dependency_overrides[require_workspace] = lambda: WorkspaceContext(
        principal, WORKSPACE, "ACTIVE"
    )
    return TestClient(app), provider, database


def post(client, legs, limit="1.30", stock=None, quantity=1):
    body = {
        "legs": [{"symbol": s, "side": side} for s, side in legs],
        "quantity": quantity,
        "limit_price": limit,
        "client_reference": "route-test",
    }
    if stock:
        body["stock_context"] = stock
    return client.post("/api/v2/option-trade-assessments", json=body)


NEW_SHAPES = [
    ("bull put credit", BULL_PUT, "1.30", None),
    ("bear call credit", BEAR_CALL, "1.30", None),
    ("protective put", PROT_PUT, "3.05", STOCK),
    ("protective put spread", PROT_SPREAD, "2.00", STOCK),
]


@pytest.mark.parametrize("name,legs,limit,stock", NEW_SHAPES, ids=[s[0] for s in NEW_SHAPES])
def test_default_workspace_refuses_new_shapes_before_any_provider_access(name, legs, limit, stock):
    client, provider, _ = make_client()
    result = post(client, legs, limit, stock).json()
    assert result["blocking_reasons"] == ["unsupported_strategy"]
    assert result["decision"] == "UNAVAILABLE"
    assert provider.calls == 0


def test_existing_shapes_still_reach_the_provider_at_defaults():
    client, provider, _ = make_client()
    result = post(client, [(C200.symbol, "buy"), (C205.symbol, "sell")], "1.10").json()
    assert provider.calls == 1 and result["decision"] == "PASS"
    assert "stock_context" not in result["trade"]


@pytest.mark.parametrize("name,legs,limit,stock", NEW_SHAPES, ids=[s[0] for s in NEW_SHAPES])
def test_enabled_workspace_loads_real_evidence_for_new_shapes(name, legs, limit, stock):
    client, provider, _ = make_client(stored={"allowed_structures": ALL_EIGHT})
    result = post(client, legs, limit, stock).json()
    assert provider.calls == 1
    assert result["decision"] == "PASS", result["remediation"]


def test_policy_control_enables_and_restores_without_touching_managed_quality():
    quality = {
        "minimum_signal_score": "30",
        "minimum_catalyst_confidence": "0.15",
        "require_price_action_confirmation": True,
    }
    gate = SimpleNamespace(
        workspace_id=WORKSPACE,
        assessment_key_id=KEY,
        enabled=False,
        revision=1,
        quality_settings=quality,
        quality_revision=1,
    )
    client, provider, database = make_client(gate=gate)
    before = client.get("/api/v2/option-trade-assessments/policy").json()
    assert "allowed_structures" not in before["policy_snapshot"]
    assert before["supported_strategies"] == ALL_EIGHT[:4]
    assert (
        post(client, BULL_PUT).json()["blocking_reasons"] == ["unsupported_strategy"]
        and provider.calls == 0
    )

    # Enable all eight through the existing authenticated workspace control (a full replace).
    full = AssessmentPolicy().model_dump(mode="json")
    full["allowed_structures"] = ALL_EIGHT
    put = client.put("/api/v1/desk/assessment-policy", json=full)
    assert put.status_code == 200, put.text
    assert put.json()["allowed_structures"] == ALL_EIGHT
    assert client.get("/api/v1/desk/assessment-policy").json()["allowed_structures"] == ALL_EIGHT
    assert any(a.action == "ASSESSMENT_POLICY_UPDATED" for a in database.audits)
    during = client.get("/api/v2/option-trade-assessments/policy").json()
    assert during["supported_strategies"] == ALL_EIGHT
    assert during["policy_snapshot"]["allowed_structures"] == ALL_EIGHT
    assert during["policy_version"] != before["policy_version"]
    assert post(client, BULL_PUT).json()["decision"] == "PASS" and provider.calls == 1

    # Restore the defaults through the same control: the agent-facing policy is byte-for-byte back.
    restore = AssessmentPolicy().model_dump(mode="json")
    assert client.put("/api/v1/desk/assessment-policy", json=restore).status_code == 200
    after = client.get("/api/v2/option-trade-assessments/policy").json()
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)
    # The managed quality settings and revision were never written by this control.
    assert gate.quality_settings == quality and gate.quality_revision == 1
    assert before["managed_quality_settings"] == after["managed_quality_settings"]


def test_a_stored_policy_without_the_new_keys_behaves_as_today():
    client, provider, _ = make_client(stored={"minimum_dte": 14})
    assert post(client, BULL_PUT).json()["blocking_reasons"] == ["unsupported_strategy"]
    assert provider.calls == 0
