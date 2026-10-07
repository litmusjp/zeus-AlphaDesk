# FastAPI dependencies are deliberately declared in defaults.
# ruff: noqa: B008
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.api.routes.agent import AgentKeyContext, require_agent_key
from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.direct_evidence import DirectEvidenceProvider, EvidenceUnavailable
from packages.connected.trade_assessment import (
    TradeRequest,
    evaluate,
    policy_metadata,
    supported_shape,
    unavailable,
)
from packages.database.models import WorkspaceRecord
from packages.security.store import CredentialStore

router = APIRouter(tags=["standalone-assessment"])


async def workspace_policy(request: Request, key: AgentKeyContext) -> AssessmentPolicy:
    async with request.app.state.database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, key.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return AssessmentPolicy.from_payload(workspace.assessment_policy)


@router.get("/option-trade-assessments/policy")
async def assessment_policy(request: Request, key: AgentKeyContext = Depends(require_agent_key)):
    return policy_metadata(await workspace_policy(request, key))


@router.post("/option-trade-assessments")
async def assess_option_trade(
    trade: TradeRequest, request: Request, key: AgentKeyContext = Depends(require_agent_key)
):
    policy = await workspace_policy(request, key)
    if not supported_shape(trade):
        return evaluate(trade, policy, None)
    factory = getattr(request.app.state, "trade_assessment_provider_factory", None)

    async def run():
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
        return await asyncio.wait_for(run(), timeout=5)
    except TimeoutError:
        return unavailable(trade, policy, "assessment_deadline_exceeded")
    except EvidenceUnavailable as exc:
        return unavailable(trade, policy, exc.code)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return unavailable(trade, policy, "invalid_market_evidence")
