from types import SimpleNamespace
from uuid import uuid4

from packages.connected.managed_review_gate import ManagedReviewGate, managed_assessment


def test_managed_contract_keeps_assessment_and_gate_authority_separate():
    gate = ManagedReviewGate(
        workspace_id=uuid4(), assessment_key_id=uuid4(), enabled=False, revision=4
    )
    result = managed_assessment({"decision": "PASS", "signal_score": 72.36}, gate)
    assert result["contract_version"] == "managed-assessment-v1"
    assert result["assessment_authority"] == "assessment_only"
    assert result["approval_gate"] == {
        "required": False,
        "scope": {
            "workspace_id": str(gate.workspace_id),
            "assessment_key_id": str(gate.assessment_key_id),
        },
        "revision": 4,
    }
    assert "human_approval_required" not in result
    assert "execution_allowed" not in result


def test_enabled_gate_withholds_pass_and_keeps_null_score_when_not_evaluated():
    gate = ManagedReviewGate(
        workspace_id=uuid4(), assessment_key_id=uuid4(), enabled=True, revision=5
    )
    result = managed_assessment(
        {"assessment_id": "attempt-id", "decision": "PASS", "signal_score": 88},
        gate,
        evaluated=False,
    )
    assert result["decision"] == "UNAVAILABLE"
    assert result["signal_score"] is None
    assert result["blocking_reasons"] == ["human_approval_required"]
    assert result["approval_gate"]["required"] is True
    assert result["assessment_id"] == "attempt-id"


def test_real_ad_evaluator_output_is_serialized_as_assessment_only_managed_pass():
    from tests.unit.test_standalone_assessment import assess

    workspace_id, key_id = uuid4(), uuid4()
    gate = ManagedReviewGate(workspace_id, key_id, False, 3)
    receipt = managed_assessment(assess(), gate)
    assert receipt["decision"] == "PASS"
    assert receipt["signal_score"] is not None
    assert receipt["contract_version"] == "managed-assessment-v1"
    assert receipt["assessment_authority"] == "assessment_only"
    assert receipt["approval_gate"]["required"] is False
    assert receipt["approval_gate"]["revision"] == 3
    assert receipt["approval_gate"]["scope"] == {
        "workspace_id": str(workspace_id),
        "assessment_key_id": str(key_id),
    }
    assert "human_approval_required" not in receipt
    assert "execution_allowed" not in receipt


class _GateSession:
    def __init__(self, row, workspace_id, profile=None):
        self.row = row
        self.workspace_id = workspace_id
        self.profile = profile or {"minimum_signal_score": "70"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, model, key):
        from packages.database.models import ManagedReviewGateRecord, WorkspaceRecord

        if model is ManagedReviewGateRecord:
            return self.row if key == (self.row.workspace_id, self.row.assessment_key_id) else None
        if model is WorkspaceRecord:
            return SimpleNamespace(assessment_policy=self.profile, status="ACTIVE")
        return None

    async def scalar(self, statement):
        values = set(statement.compile().params.values())
        if self.row.workspace_id in values and self.row.assessment_key_id in values:
            return self.row
        return None


class _GateSessions:
    def __init__(self, row, workspace_id, profile=None):
        self.row = row
        self.workspace_id = workspace_id
        self.profile = profile

    def __call__(self):
        return _GateSession(self.row, self.workspace_id, self.profile)

    def begin(self):
        return _GateSession(self.row, self.workspace_id, self.profile)


def _gate_client(*, enabled=False):
    from fastapi.testclient import TestClient

    from apps.api.app import create_app
    from apps.api.routes.agent import AgentKeyContext, require_agent_key
    from packages.configuration.settings import Settings

    workspace_id, key_id = uuid4(), uuid4()
    row = SimpleNamespace(
        workspace_id=workspace_id,
        assessment_key_id=key_id,
        enabled=enabled,
        revision=7,
        control_key_hash=__import__("hashlib").sha256(b"operator-control").hexdigest(),
    )
    app = create_app(Settings(infrastructure_checks=False))
    app.state.database = SimpleNamespace(sessions=_GateSessions(row, workspace_id))
    app.state.settings = Settings(infrastructure_checks=False)
    app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(workspace_id, key_id)
    return TestClient(app), row, workspace_id, key_id


def test_actual_gate_routes_enforce_control_binding_strict_cas_and_persist_across_profile_changes():
    from packages.security.agent_keys import hash_agent_key

    client, row, _workspace_id, _key_id = _gate_client()
    base = "/api/v2/option-trade-assessments/managed-review-gate"
    assert client.put(base, json={"required": True, "expected_revision": 7}).status_code == 403
    wrong = client.put(
        base,
        json={"required": True, "expected_revision": 7},
        headers={"X-AlphaDesk-Control-Key": "wrong-control"},
    )
    assert wrong.status_code == 403
    malformed = client.put(
        base,
        json={"required": 1, "expected_revision": True},
        headers={"X-AlphaDesk-Control-Key": "operator-control"},
    )
    assert malformed.status_code == 422
    headers = {"X-AlphaDesk-Control-Key": "operator-control"}
    initial = client.get(base)
    assert initial.headers["cache-control"].startswith("no-store")
    assert initial.headers["pragma"] == "no-cache"
    nonpositive = client.put(base, json={"required": True, "expected_revision": 0}, headers=headers)
    assert nonpositive.status_code == 422
    response = client.put(base, json={"required": True, "expected_revision": 7}, headers=headers)
    assert response.status_code == 200
    assert response.json()["approval_gate"]["required"] is True
    assert response.json()["approval_gate"]["revision"] == 8
    assert row.enabled is True and row.revision == 8
    stale = client.put(base, json={"required": False, "expected_revision": 7}, headers=headers)
    assert stale.status_code == 409
    assert row.enabled is True and row.revision == 8

    # The workspace profile may be replaced independently; gate state is a separate DB row.
    new_profile = {"minimum_signal_score": "91"}
    client.app.state.database.sessions.profile = new_profile
    read = client.get(base)
    assert read.status_code == 200
    assert read.json()["approval_gate"]["revision"] == 8
    assert read.json()["approval_gate"]["required"] is True
    assert hash_agent_key("operator-control") == row.control_key_hash


def test_actual_gate_routes_scope_wrong_key_unknown_gate_and_policy_cache_headers():
    from apps.api.routes.agent import AgentKeyContext, require_agent_key

    client, row, workspace_id, key_id = _gate_client()
    base = "/api/v2/option-trade-assessments/managed-review-gate"
    client.app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(uuid4(), key_id)
    wrong_workspace = client.put(
        base,
        json={"required": True, "expected_revision": 7},
        headers={"X-AlphaDesk-Control-Key": "operator-control"},
    )
    assert wrong_workspace.status_code == 403
    assert row.enabled is False and row.revision == 7
    client.app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(
        workspace_id, uuid4()
    )
    assert client.get(base).status_code == 503
    policy = client.get("/api/v2/option-trade-assessments/policy")
    assert policy.headers["cache-control"].startswith("no-store")


def test_locked_actual_assessment_route_does_not_call_provider_and_returns_unavailable_null_score():
    client, _row, _workspace_id, _key_id = _gate_client(enabled=True)
    provider_calls = []
    client.app.state.trade_assessment_provider_factory = lambda _key: provider_calls.append(True)
    expiry = "261120"
    response = client.post(
        "/api/v2/option-trade-assessments",
        json={
            "legs": [{"symbol": f"AAPL{expiry}C00200000", "side": "buy"}],
            "quantity": 1,
            "limit_price": "2",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
    body = response.json()
    assert body["decision"] == "UNAVAILABLE"
    assert body["signal_score"] is None
    assert body["blocking_reasons"] == ["human_approval_required"]
    assert body["assessment_id"]
    assert body["approval_gate"]["required"] is True
    assert provider_calls == []


def test_managed_policy_route_has_distinct_contract_and_legacy_route_remains_legacy():
    client, _row, _workspace_id, _key_id = _gate_client()
    managed = client.get("/api/v2/option-trade-assessments/policy")
    assert managed.status_code == 200
    body = managed.json()
    assert body["contract_version"] == "managed-assessment-v1"
    assert body["approval_gate"]["required"] is False
    assert body["approval_gate"]["revision"] == 7
    assert "human_approval_required" not in body and "execution_allowed" not in body

    # A key without an explicitly provisioned row receives the unchanged legacy shape.
    from apps.api.routes.agent import AgentKeyContext, require_agent_key

    client.app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(uuid4(), uuid4())
    legacy = client.get("/api/v2/option-trade-assessments/policy").json()
    assert "contract_version" not in legacy and legacy["human_approval_required"] is True
