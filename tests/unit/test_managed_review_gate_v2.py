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
            return (
                self.row
                if self.row is not None
                and key == (self.row.workspace_id, self.row.assessment_key_id)
                else None
            )
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


def test_managed_assessment_rechecks_gate_after_provider_work_and_finalizes_unavailable():
    import asyncio
    import threading

    client, row, _workspace_id, _key_id = _gate_client()
    started, release = threading.Event(), threading.Event()

    class Provider:
        async def get_evidence(self, _trade):
            from packages.connected.direct_evidence import EvidenceUnavailable

            started.set()
            assert await asyncio.to_thread(release.wait, 2)
            raise EvidenceUnavailable("market_data_not_configured")

    client.app.state.trade_assessment_provider_factory = lambda _key: Provider()
    client.app.state.credential_cipher = None
    result = {}

    def request_assessment():
        result["response"] = client.post(
            "/api/v2/option-trade-assessments",
            json={
                "legs": [{"symbol": "AAPL261120C00200000", "side": "buy"}],
                "quantity": 1,
                "limit_price": "2",
            },
        )

    worker = threading.Thread(target=request_assessment)
    worker.start()
    try:
        assert started.wait(2)
        row.enabled = True
        row.revision += 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    body = result["response"].json()
    assert body["contract_version"] == "managed-assessment-v1"
    assert body["approval_gate"]["required"] is True
    assert body["approval_gate"]["revision"] == row.revision
    assert body["decision"] == "UNAVAILABLE" and body["signal_score"] is None
    assert body["blocking_reasons"] == ["human_approval_required"]


def test_managed_unavailable_early_return_keeps_managed_contract_and_reason():
    client, _row, _workspace_id, _key_id = _gate_client()
    client.app.state.credential_cipher = None
    client.app.state.trade_assessment_provider_factory = None
    body = client.post(
        "/api/v2/option-trade-assessments",
        json={
            "legs": [{"symbol": "AAPL261120C00200000", "side": "buy"}],
            "quantity": 1,
            "limit_price": "2",
        },
    ).json()
    assert body["contract_version"] == "managed-assessment-v1"
    assert body["approval_gate"]["required"] is False
    assert body["decision"] == "UNAVAILABLE" and body["signal_score"] is None
    assert body["blocking_reasons"] == ["market_data_unavailable"]


def test_managed_off_finalizes_missing_unverified_and_malformed_credentials(monkeypatch):
    import apps.api.routes.assessment as route

    def check_kind(kind):
        client, _row, _workspace_id, _key_id = _gate_client()
        record = (
            None
            if kind == "missing"
            else SimpleNamespace(
                enabled=True,
                validation_status="PENDING" if kind == "unverified" else "VERIFIED",
                key_version=1,
                nonce=b"fixture",
                ciphertext=b"fixture",
                fingerprint="fixture",
            )
        )

        class Store:
            def __init__(self, *_args):
                pass

            async def get(self, *_args):
                return record

        class Cipher:
            def decrypt(self, *_args):
                return {"api_key": "wrong-name", "secret_key": "fixture"}

        monkeypatch.setattr(route, "CredentialStore", Store)
        client.app.state.credential_cipher = Cipher()
        body = client.post(
            "/api/v2/option-trade-assessments",
            json={
                "legs": [{"symbol": "AAPL261120C00200000", "side": "buy"}],
                "quantity": 1,
                "limit_price": "2",
            },
        ).json()
        assert body["contract_version"] == "managed-assessment-v1"
        assert body["approval_gate"]["required"] is False
        assert body["decision"] == "UNAVAILABLE" and body["signal_score"] is None
        assert body["blocking_reasons"] == ["market_data_not_configured"]

    for kind in ("missing", "unverified", "malformed"):
        check_kind(kind)


def test_managed_off_finalizes_provider_error_and_timeout(monkeypatch):
    import apps.api.routes.assessment as route
    from packages.connected.direct_evidence import EvidenceUnavailable

    class FailingProvider:
        async def get_evidence(self, _trade):
            raise EvidenceUnavailable("managed_fixture_provider_error")

    client, _row, _workspace_id, _key_id = _gate_client()
    client.app.state.trade_assessment_provider_factory = lambda _key: FailingProvider()
    body = client.post(
        "/api/v2/option-trade-assessments",
        json={
            "legs": [{"symbol": "AAPL261120C00200000", "side": "buy"}],
            "quantity": 1,
            "limit_price": "2",
        },
    ).json()
    assert body["contract_version"] == "managed-assessment-v1"
    assert body["approval_gate"]["required"] is False
    assert body["blocking_reasons"] == ["managed_fixture_provider_error"]

    async def expired(awaitable, **_kwargs):
        awaitable.close()
        raise TimeoutError("fixture-private-detail")

    monkeypatch.setattr(route.asyncio, "wait_for", expired)
    client, _row, _workspace_id, _key_id = _gate_client()
    client.app.state.trade_assessment_provider_factory = lambda _key: FailingProvider()
    response = client.post(
        "/api/v2/option-trade-assessments",
        json={
            "legs": [{"symbol": "AAPL261120C00200000", "side": "buy"}],
            "quantity": 1,
            "limit_price": "2",
        },
    )
    body = response.json()
    assert body["contract_version"] == "managed-assessment-v1"
    assert body["approval_gate"]["required"] is False
    assert body["blocking_reasons"] == ["assessment_deadline_exceeded"]
    assert "fixture-private-detail" not in response.text


def test_managed_passing_assessment_is_withheld_when_gate_turns_on_during_evaluation():
    import asyncio
    import threading

    from tests.api.test_standalone_assessment import _passing_evidence, trade

    client, row, _workspace_id, _key_id = _gate_client()
    proposal = trade()

    class Provider:
        def __init__(self, started=None, release=None):
            self.started, self.release = started, release

        async def get_evidence(self, request):
            if self.started:
                self.started.set()
                await asyncio.to_thread(self.release.wait, 2)
            return _passing_evidence(request)

    client.app.state.trade_assessment_provider_factory = lambda _key: Provider()
    baseline = client.post("/api/v2/option-trade-assessments", json=proposal).json()
    assert baseline["decision"] == "PASS"
    assert baseline["approval_gate"]["required"] is False

    started, release = threading.Event(), threading.Event()
    client.app.state.trade_assessment_provider_factory = lambda _key: Provider(started, release)
    result = {}

    def request_assessment():
        result["response"] = client.post("/api/v2/option-trade-assessments", json=proposal)

    worker = threading.Thread(target=request_assessment)
    worker.start()
    try:
        assert started.wait(2)
        row.enabled = True
        row.revision += 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    body = result["response"].json()
    assert body["decision"] == "UNAVAILABLE" and body["signal_score"] is None
    assert body["blocking_reasons"] == ["human_approval_required"]
    assert body["approval_gate"] == {
        "required": True,
        "revision": row.revision,
        "scope": body["approval_gate"]["scope"],
    }


def test_disappearing_managed_binding_reports_unknown_without_fabricating_lock():
    import asyncio
    import threading

    from tests.api.test_standalone_assessment import _passing_evidence, trade

    client, _row, workspace_id, key_id = _gate_client()
    started, release = threading.Event(), threading.Event()

    class Provider:
        async def get_evidence(self, request):
            started.set()
            await asyncio.to_thread(release.wait, 2)
            return _passing_evidence(request)

    client.app.state.trade_assessment_provider_factory = lambda _key: Provider()
    result = {}

    def request_assessment():
        result["response"] = client.post("/api/v2/option-trade-assessments", json=trade())

    worker = threading.Thread(target=request_assessment)
    worker.start()
    try:
        assert started.wait(2)
        client.app.state.database.sessions.row = None
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    body = result["response"].json()
    assert body["decision"] == "UNAVAILABLE" and body["signal_score"] is None
    assert body["blocking_reasons"] == ["managed_policy_unavailable"]
    assert body["approval_gate"]["required"] is None
    assert body["approval_gate"]["status"] == "unknown"
    assert body["approval_gate"]["scope"] == {
        "workspace_id": str(workspace_id),
        "assessment_key_id": str(key_id),
    }
    assert "human_approval_required" not in body["blocking_reasons"]
