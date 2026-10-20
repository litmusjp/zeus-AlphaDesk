from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.unit.test_managed_review_gate_v2 import _gate_client

BASE = "/api/v2/option-trade-assessments/managed-quality-settings"
HEADERS = {"X-AlphaDesk-Control-Key": "operator-control"}


def test_quality_settings_read_write_is_scoped_strict_cas_and_persistent():
    client, row, _, _ = _gate_client()
    row.quality_settings = None
    row.quality_revision = 1

    initial = client.get(BASE)
    assert initial.status_code == 200
    assert initial.json() == {
        "contract_version": "managed-assessment-v1",
        "scope": {
            "workspace_id": str(row.workspace_id),
            "assessment_key_id": str(row.assessment_key_id),
        },
        "defaults": {
            "minimum_signal_score": "30",
            "minimum_catalyst_confidence": "0.20",
            "require_price_action_confirmation": True,
        },
        "effective": {
            "minimum_signal_score": "70",
            "minimum_catalyst_confidence": "0.60",
            "require_price_action_confirmation": True,
        },
        "revision": 1,
    }

    assert (
        client.put(
            BASE,
            json={
                "minimum_signal_score": 25,
                "minimum_catalyst_confidence": 0.15,
                "require_price_action_confirmation": False,
                "expected_revision": 1,
            },
        ).status_code
        == 403
    )
    assert (
        client.put(
            BASE,
            json={
                "minimum_signal_score": 25,
                "minimum_catalyst_confidence": 0.15,
                "require_price_action_confirmation": False,
                "expected_revision": 1,
            },
            headers={"X-AlphaDesk-Control-Key": "wrong-control"},
        ).status_code
        == 403
    )

    payload = {
        "minimum_signal_score": 25,
        "minimum_catalyst_confidence": 0.15,
        "require_price_action_confirmation": False,
        "expected_revision": 1,
    }
    updated = client.put(BASE, json=payload, headers=HEADERS)
    assert updated.status_code == 200
    assert updated.json()["effective"] == {
        "minimum_signal_score": "25",
        "minimum_catalyst_confidence": "0.15",
        "require_price_action_confirmation": False,
    }
    assert updated.json()["scope"] == initial.json()["scope"]
    assert updated.json()["revision"] == 2
    assert row.quality_settings == {
        "minimum_signal_score": "25",
        "minimum_catalyst_confidence": "0.15",
        "require_price_action_confirmation": False,
    }

    stale = client.put(BASE, json={**payload, "expected_revision": 1}, headers=HEADERS)
    assert stale.status_code == 409
    reread = client.get(BASE)
    assert reread.json()["effective"] == updated.json()["effective"]
    assert reread.json()["revision"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {
            "minimum_signal_score": 25,
            "minimum_catalyst_confidence": 0.15,
            "require_price_action_confirmation": 0,
            "expected_revision": 1,
        },
        {
            "minimum_signal_score": True,
            "minimum_catalyst_confidence": 0.15,
            "require_price_action_confirmation": True,
            "expected_revision": 1,
        },
        {
            "minimum_signal_score": -1,
            "minimum_catalyst_confidence": 0.15,
            "require_price_action_confirmation": True,
            "expected_revision": 1,
        },
        {
            "minimum_signal_score": 101,
            "minimum_catalyst_confidence": 0.15,
            "require_price_action_confirmation": True,
            "expected_revision": 1,
        },
        {
            "minimum_signal_score": 25,
            "minimum_catalyst_confidence": -0.01,
            "require_price_action_confirmation": True,
            "expected_revision": 1,
        },
        {
            "minimum_signal_score": 25,
            "minimum_catalyst_confidence": 1.01,
            "require_price_action_confirmation": True,
            "expected_revision": 1,
        },
    ],
)
def test_quality_settings_reject_invalid_types_and_ranges(payload):
    client, row, _, _ = _gate_client()
    row.quality_revision = 1
    row.quality_settings = None
    response = client.put(BASE, json=payload, headers=HEADERS)
    assert response.status_code == 422
    assert row.quality_revision == 1
    assert row.quality_settings is None


def test_quality_settings_rejects_cross_scope_key_even_with_operator_secret():
    client, row, _workspace_id, key_id = _gate_client()
    row.quality_settings = None
    from apps.api.routes.agent import AgentKeyContext

    client.app.dependency_overrides.clear()
    from apps.api.routes.agent import require_agent_key

    client.app.dependency_overrides[require_agent_key] = lambda: AgentKeyContext(uuid4(), key_id)
    response = client.put(
        BASE,
        json={
            "minimum_signal_score": 25,
            "minimum_catalyst_confidence": 0.15,
            "require_price_action_confirmation": True,
            "expected_revision": 1,
        },
        headers=HEADERS,
    )
    assert response.status_code == 403
    assert row.quality_settings is None


def test_quality_settings_override_standalone_environment_score_for_managed_assessment():
    from packages.configuration.settings import Settings

    client, row, _, _ = _gate_client()
    client.app.state.settings = Settings(
        infrastructure_checks=False, standalone_minimum_signal_score="42"
    )
    row.quality_settings = {
        "minimum_signal_score": "25",
        "minimum_catalyst_confidence": "0.15",
        "require_price_action_confirmation": True,
    }
    row.quality_revision = 4

    from tests.api.test_standalone_assessment import _passing_evidence, trade

    class Provider:
        async def get_evidence(self, proposal):
            return _passing_evidence(proposal)

    client.app.state.trade_assessment_provider_factory = lambda _key: Provider()
    response = client.post("/api/v2/option-trade-assessments", json=trade())
    assert response.status_code == 200
    body = response.json()
    assert body["minimum_passing_score"] == "25"
    assert body["decision"] == "PASS"
    assert ":quality:4" in body["policy_version"]
    from packages.connected.assessment_policy import AssessmentPolicy

    snapshot = AssessmentPolicy.model_validate(
        body["policy_snapshot"], context={"managed_quality_receipt": True}
    )
    assert snapshot.minimum_signal_score == Decimal("25")
    assert snapshot.minimum_catalyst_confidence == Decimal("0.15")


def test_quality_settings_change_during_evaluation_withholds_old_decision():
    from tests.api.test_standalone_assessment import _passing_evidence, trade

    client, row, _, _ = _gate_client()
    row.quality_settings = {
        "minimum_signal_score": "25",
        "minimum_catalyst_confidence": "0.15",
        "require_price_action_confirmation": True,
    }
    row.quality_revision = 2
    started, release = threading.Event(), threading.Event()

    class Provider:
        async def get_evidence(self, proposal):
            started.set()
            await asyncio.to_thread(release.wait, 2)
            return _passing_evidence(proposal)

    client.app.state.trade_assessment_provider_factory = lambda _key: Provider()
    result = {}

    def request_assessment():
        result["response"] = client.post("/api/v2/option-trade-assessments", json=trade())

    worker = threading.Thread(target=request_assessment)
    worker.start()
    try:
        assert started.wait(2)
        row.quality_settings = {
            "minimum_signal_score": "30",
            "minimum_catalyst_confidence": "0.20",
            "require_price_action_confirmation": True,
        }
        row.quality_revision += 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    body = result["response"].json()
    assert body["decision"] == "UNAVAILABLE"
    assert body["blocking_reasons"] == ["managed_quality_policy_changed"]
    assert ":quality:3" in body["policy_version"]


def test_managed_quality_settings_participate_in_receipt_policy_fingerprint():
    from packages.connected.assessment_policy import AssessmentPolicy
    from packages.connected.trade_assessment import policy_metadata

    baseline = policy_metadata(AssessmentPolicy())
    lowered = AssessmentPolicy().model_copy(
        update={
            "minimum_signal_score": Decimal("25"),
            "minimum_catalyst_confidence": Decimal("0.15"),
        }
    )
    different = policy_metadata(lowered)
    assert baseline["policy_version"] != different["policy_version"]
    confirmation_off = policy_metadata(
        AssessmentPolicy().model_copy(update={"require_price_action_confirmation": False})
    )
    assert baseline["policy_version"] != confirmation_off["policy_version"]
    confidence_changed = policy_metadata(
        AssessmentPolicy().model_copy(update={"minimum_catalyst_confidence": Decimal("0.15")})
    )
    assert baseline["policy_version"] != confidence_changed["policy_version"]


def test_price_action_confirmation_off_only_removes_eligibility_veto():
    from dataclasses import replace

    from packages.connected.assessment_policy import AssessmentPolicy
    from tests.unit.test_standalone_assessment import NOW, assess, evidence

    ev = evidence()
    ev = replace(ev, features=ev.features.model_copy(update={"price_momentum": Decimal("-.8")}))
    base = AssessmentPolicy().model_copy(
        update={
            "minimum_signal_score": Decimal("25"),
            "pre_scan_minimum_signal_score": Decimal("20"),
            "minimum_catalyst_confidence": Decimal("0.15"),
            "pre_scan_minimum_catalyst_confidence": Decimal("0.10"),
        }
    )
    on_policy = base.model_copy(update={"require_price_action_confirmation": True})
    off_policy = base.model_copy(update={"require_price_action_confirmation": False})
    on = assess(ev=ev, policy=on_policy)
    off = assess(ev=ev, policy=off_policy)
    on_again = assess(ev=ev, policy=on_policy)

    assert on["decision"] == "FAIL" and "price_action_not_confirming" in on["blocking_reasons"]
    assert off["signal_score"] == on["signal_score"] == on_again["signal_score"]
    assert off["decision"] == "PASS"
    assert off["price_action_confirmation"] == "not_required_by_operator_policy"
    assert "price_action_not_confirming" not in off["blocking_reasons"]
    assert off["policy_version"] != on["policy_version"]
    from packages.connected.trade_assessment import validate_pass_receipt
    from tests.unit.test_standalone_assessment import proposal

    assert validate_pass_receipt(off, proposal(), now=NOW)
    weak = replace(
        ev, features=ev.features.model_copy(update={"catalyst_confidence": Decimal(".1")})
    )
    assert "weak_catalyst_confidence" in assess(ev=weak, policy=off_policy)["blocking_reasons"]


def test_workspace_policy_payload_cannot_change_managed_only_controls():
    from packages.connected.assessment_policy import AssessmentPolicy

    policy = AssessmentPolicy.from_payload(
        {"require_price_action_confirmation": False, "managed_quality_override": True}
    )
    assert policy.require_price_action_confirmation is True
    assert "managed_quality_override" not in policy.model_dump()


def test_managed_receipt_context_skips_only_underlying_threshold_relationships():
    from packages.connected.assessment_policy import AssessmentPolicy

    with pytest.raises(ValueError, match="maximum_dte"):
        AssessmentPolicy.model_validate(
            {"minimum_dte": 60, "maximum_dte": 30},
            context={"managed_quality_receipt": True},
        )
    with pytest.raises(ValueError, match="spread ratio"):
        AssessmentPolicy.model_validate(
            {"pre_scan_max_spread_ratio": "0.10", "execution_max_spread_ratio": "0.20"},
            context={"managed_quality_receipt": True},
        )


def test_public_assessment_policy_input_cannot_bypass_relationship_validation():
    from apps.api.routes.desk import AssessmentPolicyInput

    accepted = AssessmentPolicyInput.model_validate({"managed_quality_override": True})
    assert "managed_quality_override" not in accepted.model_dump()

    with pytest.raises(ValueError, match="pre-scan signal score"):
        AssessmentPolicyInput.model_validate(
            {
                "minimum_signal_score": "25",
                "pre_scan_minimum_signal_score": "50",
                "managed_quality_override": True,
            }
        )
