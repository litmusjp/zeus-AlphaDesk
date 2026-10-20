# FastAPI dependencies are deliberately declared in defaults.
# ruff: noqa: B008
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import select

from apps.api.routes.agent import AgentKeyContext, require_agent_key
from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.direct_evidence import DirectEvidenceProvider, EvidenceUnavailable
from packages.connected.managed_review_gate import (
    ManagedQualitySettings,
    ManagedReviewGate,
    managed_assessment,
    managed_quality_payload,
)
from packages.connected.trade_assessment import (
    TradeRequest,
    evaluate,
    policy_metadata,
    supported_shape,
    unavailable,
)
from packages.database.models import ManagedReviewGateRecord, WorkspaceRecord
from packages.security.agent_keys import hash_agent_key
from packages.security.store import CredentialStore

router = APIRouter(tags=["standalone-assessment"])


async def _managed_gate(request: Request, key: AgentKeyContext) -> ManagedReviewGate | None:
    gate, _quality, _revision = await _managed_control(request, key)
    return gate


async def _managed_control(
    request: Request, key: AgentKeyContext
) -> tuple[ManagedReviewGate | None, ManagedQualitySettings | None, int]:
    async with request.app.state.database.sessions() as session:
        row = await session.get(ManagedReviewGateRecord, (key.workspace_id, key.key_id))
    if row is None or not all(
        hasattr(row, name) for name in ("workspace_id", "assessment_key_id", "enabled", "revision")
    ):
        return None, None, 1
    gate = ManagedReviewGate(row.workspace_id, row.assessment_key_id, row.enabled, row.revision)
    raw_settings = getattr(row, "quality_settings", None)
    try:
        settings = ManagedQualitySettings.model_validate(raw_settings) if raw_settings else None
        revision = getattr(row, "quality_revision", 1) or 1
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid managed quality revision")
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Managed quality policy is invalid") from None
    return gate, settings, revision


async def workspace_policy(request: Request, key: AgentKeyContext) -> AssessmentPolicy:
    async with request.app.state.database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, key.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return AssessmentPolicy.from_payload(workspace.assessment_policy)


async def standalone_policy(
    request: Request,
    key: AgentKeyContext,
    quality_settings: ManagedQualitySettings | None = None,
) -> AssessmentPolicy:
    policy = await workspace_policy(request, key)
    if quality_settings is not None:
        return policy.model_copy(
            update={
                "minimum_signal_score": quality_settings.minimum_signal_score,
                "minimum_catalyst_confidence": quality_settings.minimum_catalyst_confidence,
                "require_price_action_confirmation": (
                    quality_settings.require_price_action_confirmation
                ),
            }
        )
    override = request.app.state.settings.standalone_minimum_signal_score
    if override is not None:
        return policy.model_copy(update={"minimum_signal_score": Decimal(override)})
    return policy


@router.get("/option-trade-assessments/policy")
async def assessment_policy(
    request: Request, response: Response, key: AgentKeyContext = Depends(require_agent_key)
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    gate, quality, quality_revision = await _managed_control(request, key)
    result = policy_metadata(await standalone_policy(request, key, quality))
    if gate:
        result.pop("execution_allowed", None)
        result.pop("human_approval_required", None)
        result.update(
            contract_version="managed-assessment-v1",
            assessment_authority="assessment_only",
            approval_gate={
                "required": gate.enabled,
                "revision": gate.revision,
                "scope": {
                    "workspace_id": str(gate.workspace_id),
                    "assessment_key_id": str(gate.assessment_key_id),
                },
            },
        )
        result["policy_version"] += f":managed:{gate.revision}:{gate.enabled}"
        result["policy_version"] += f":quality:{quality_revision}"
        result["managed_quality_settings"] = _quality_view(
            result["policy_snapshot"], quality, quality_revision
        )
    return result


def _quality_view(
    policy_snapshot: dict[str, Any], quality: ManagedQualitySettings | None, revision: int
) -> dict[str, Any]:
    defaults = managed_quality_payload(ManagedQualitySettings())
    effective = (
        managed_quality_payload(quality)
        if quality is not None
        else {
            "minimum_signal_score": str(policy_snapshot["minimum_signal_score"]),
            "minimum_catalyst_confidence": str(policy_snapshot["minimum_catalyst_confidence"]),
            "require_price_action_confirmation": True,
        }
    )
    return {"defaults": defaults, "effective": effective, "revision": revision}


@router.get("/option-trade-assessments/managed-quality-settings")
async def read_managed_quality_settings(
    request: Request,
    response: Response,
    key: AgentKeyContext = Depends(require_agent_key),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    gate, quality, revision = await _managed_control(request, key)
    if gate is None:
        raise HTTPException(status_code=503, detail="Managed quality policy is not provisioned")
    policy = await standalone_policy(request, key, quality)
    return {
        "contract_version": "managed-assessment-v1",
        "scope": {
            "workspace_id": str(gate.workspace_id),
            "assessment_key_id": str(gate.assessment_key_id),
        },
        **_quality_view(policy.model_dump(mode="json"), quality, revision),
    }


@router.put("/option-trade-assessments/managed-quality-settings")
async def write_managed_quality_settings(
    payload: dict[str, Any],
    request: Request,
    key: AgentKeyContext = Depends(require_agent_key),
    control_key: str | None = Header(default=None, alias="X-AlphaDesk-Control-Key"),
) -> dict[str, Any]:
    if (
        set(payload)
        != {
            "minimum_signal_score",
            "minimum_catalyst_confidence",
            "require_price_action_confirmation",
            "expected_revision",
        }
        or type(payload.get("expected_revision")) is not int
        or payload["expected_revision"] < 1
    ):
        raise HTTPException(
            status_code=422, detail="Complete quality settings and expected_revision are required"
        )
    try:
        settings = ManagedQualitySettings.model_validate(
            {
                name: payload[name]
                for name in (
                    "minimum_signal_score",
                    "minimum_catalyst_confidence",
                    "require_price_action_confirmation",
                )
            }
        )
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid managed quality settings") from None
    if not control_key:
        raise HTTPException(status_code=403, detail="Scoped operator control credential required")
    async with request.app.state.database.sessions.begin() as session:
        row = await session.scalar(
            select(ManagedReviewGateRecord)
            .where(
                ManagedReviewGateRecord.workspace_id == key.workspace_id,
                ManagedReviewGateRecord.assessment_key_id == key.key_id,
            )
            .with_for_update()
        )
        if row is None or row.control_key_hash != hash_agent_key(control_key):
            raise HTTPException(
                status_code=403, detail="Control credential is not bound to this managed connection"
            )
        revision = getattr(row, "quality_revision", 1) or 1
        if revision != payload["expected_revision"]:
            raise HTTPException(status_code=409, detail="Stale managed quality policy revision")
        new_settings = managed_quality_payload(settings)
        if getattr(row, "quality_settings", None) != new_settings:
            row.quality_settings = new_settings
            row.quality_revision = revision + 1
            row.updated_at = datetime.now(UTC)
        result = {
            "contract_version": "managed-assessment-v1",
            "scope": {
                "workspace_id": str(row.workspace_id),
                "assessment_key_id": str(row.assessment_key_id),
            },
            "defaults": managed_quality_payload(ManagedQualitySettings()),
            "effective": new_settings,
            "revision": row.quality_revision,
        }
    return result


@router.get("/option-trade-assessments/managed-review-gate")
async def read_managed_review_gate(
    request: Request, response: Response, key: AgentKeyContext = Depends(require_agent_key)
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    gate = await _managed_gate(request, key)
    if gate is None:
        raise HTTPException(status_code=503, detail="Managed review policy is not provisioned")
    return {
        "contract_version": "managed-assessment-v1",
        "approval_gate": {
            "required": gate.enabled,
            "revision": gate.revision,
            "scope": {
                "workspace_id": str(gate.workspace_id),
                "assessment_key_id": str(gate.assessment_key_id),
            },
        },
    }


@router.put("/option-trade-assessments/managed-review-gate")
async def write_managed_review_gate(
    payload: dict[str, Any],
    request: Request,
    key: AgentKeyContext = Depends(require_agent_key),
    control_key: str | None = Header(default=None, alias="X-AlphaDesk-Control-Key"),
) -> dict[str, Any]:
    if (
        set(payload) != {"required", "expected_revision"}
        or type(payload.get("required")) is not bool
        or type(payload.get("expected_revision")) is not int
        or payload.get("expected_revision", 0) < 1
    ):
        raise HTTPException(
            status_code=422, detail="required boolean and expected_revision integer are required"
        )
    if not control_key:
        raise HTTPException(status_code=403, detail="Scoped operator control credential required")
    async with request.app.state.database.sessions.begin() as session:
        row = await session.scalar(
            select(ManagedReviewGateRecord)
            .where(
                ManagedReviewGateRecord.workspace_id == key.workspace_id,
                ManagedReviewGateRecord.assessment_key_id == key.key_id,
            )
            .with_for_update()
        )
        if row is None or row.control_key_hash != hash_agent_key(control_key):
            raise HTTPException(
                status_code=403, detail="Control credential is not bound to this managed connection"
            )
        if row.revision != payload["expected_revision"]:
            raise HTTPException(status_code=409, detail="Stale managed review policy revision")
        if row.enabled != payload["required"]:
            row.enabled = payload["required"]
            row.revision += 1
            row.updated_at = datetime.now(UTC)
        result = {
            "contract_version": "managed-assessment-v1",
            "approval_gate": {
                "required": row.enabled,
                "revision": row.revision,
                "scope": {
                    "workspace_id": str(row.workspace_id),
                    "assessment_key_id": str(row.assessment_key_id),
                },
            },
        }
    return result


@router.post("/option-trade-assessments")
async def assess_option_trade(
    trade: TradeRequest,
    request: Request,
    response: Response,
    key: AgentKeyContext = Depends(require_agent_key),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    gate, quality, quality_revision = await _managed_control(request, key)
    policy = await standalone_policy(request, key, quality)
    if gate and gate.enabled:
        result = managed_assessment(
            unavailable(trade, policy, "human_approval_required"), gate, evaluated=False
        )
        result["policy_version"] += f":quality:{quality_revision}"
        return result

    async def finalize(result: dict[str, Any], *, evaluated: bool = True) -> dict[str, Any]:
        if gate is None:
            return result
        current_gate, current_quality, current_quality_revision = await _managed_control(
            request, key
        )
        if current_gate is None:
            value = managed_assessment(
                unavailable(trade, policy, "managed_policy_unavailable"),
                gate,
                evaluated=False,
            )
            value["approval_gate"]["required"] = None
            value["approval_gate"]["status"] = "unknown"
            base_policy_version = result.get("policy_version", "")
            value["policy_version"] = f"{base_policy_version}:managed:{gate.revision}:unknown"
            return value
        if current_gate.enabled:
            return managed_assessment(result, current_gate, evaluated=evaluated)
        if current_quality_revision != quality_revision:
            current_policy = await standalone_policy(request, key, current_quality)
            changed = unavailable(trade, current_policy, "managed_quality_policy_changed")
            changed["blocking_reasons"] = ["managed_quality_policy_changed"]
            changed["remediation"] = [
                "The managed quality policy changed during evaluation; submit a new assessment."
            ]
            value = managed_assessment(changed, current_gate, evaluated=False)
            value["policy_version"] += f":quality:{current_quality_revision}"
            return value
        value = managed_assessment(result, current_gate, evaluated=evaluated)
        value["policy_version"] += f":quality:{quality_revision}"
        return value

    if not supported_shape(trade):
        result = evaluate(trade, policy, None)
        return await finalize(result)
    factory = getattr(request.app.state, "trade_assessment_provider_factory", None)

    async def run() -> dict[str, Any]:
        if factory is not None:
            provider = factory(key)
        else:
            cipher = request.app.state.credential_cipher
            if cipher is None:
                return unavailable(trade, policy)
            store = CredentialStore(request.app.state.database.sessions, cipher)
            record = await store.get(key.workspace_id, "ALPACA_PAPER")
            if record is None or not record.enabled or record.validation_status != "VERIFIED":
                return unavailable(trade, policy, "market_data_not_configured")
            # Decrypt the exact verified, enabled record; no cross-workspace fallback.
            credentials = cipher.decrypt(
                key.workspace_id,
                "ALPACA_PAPER",
                record.key_version,
                record.nonce,
                record.ciphertext,
            )
            if (
                not isinstance(credentials, dict)
                or not isinstance(credentials.get("api_key_id"), str)
                or not credentials["api_key_id"].strip()
                or not isinstance(credentials.get("secret_key"), str)
                or not credentials["secret_key"].strip()
            ):
                return unavailable(trade, policy, "market_data_not_configured")
            cache = getattr(request.app.state, "trade_assessment_providers", None)
            if cache is None:
                cache = request.app.state.trade_assessment_providers = {}
            cache_key = (key.workspace_id, record.fingerprint)
            provider = cache.get(cache_key)
            if provider is None:
                provider = DirectEvidenceProvider(
                    credentials["api_key_id"], credentials["secret_key"]
                )
                if len(cache) >= 128:
                    cache.pop(next(iter(cache)))
                cache[cache_key] = provider
        evidence = await provider.get_evidence(trade)
        return evaluate(trade, policy, evidence)

    try:
        result = await asyncio.wait_for(run(), timeout=5)
    except TimeoutError:
        result = unavailable(trade, policy, "assessment_deadline_exceeded")
        evaluated = False
    except EvidenceUnavailable as exc:
        result = unavailable(trade, policy, exc.code)
        evaluated = False
    except (ValueError, KeyError, TypeError, ArithmeticError):
        result = unavailable(trade, policy, "invalid_market_evidence")
        evaluated = False
    else:
        evaluated = True
    return await finalize(result, evaluated=evaluated)
