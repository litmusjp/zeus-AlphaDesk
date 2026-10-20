"""Scoped autonomous-entry lock for explicitly managed agent connections."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool


@dataclass(frozen=True)
class ManagedReviewGate:
    workspace_id: UUID
    assessment_key_id: UUID
    enabled: bool
    revision: int


class ManagedQualitySettings(BaseModel):
    """Strict, standalone assessment quality controls for one managed connection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_signal_score: Decimal = Field(default=Decimal("30"), ge=0, le=100)
    minimum_catalyst_confidence: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    require_price_action_confirmation: StrictBool = True


def managed_quality_payload(settings: ManagedQualitySettings) -> dict[str, Any]:
    return {
        "minimum_signal_score": str(settings.minimum_signal_score),
        "minimum_catalyst_confidence": str(settings.minimum_catalyst_confidence),
        "require_price_action_confirmation": settings.require_price_action_confirmation,
    }


def managed_assessment(
    result: dict[str, Any], gate: ManagedReviewGate, *, evaluated: bool = True
) -> dict[str, Any]:
    """Attach a distinct managed authority contract; a locked gate has no actionable PASS."""
    value = dict(result)
    value.pop("execution_allowed", None)
    value.pop("human_approval_required", None)
    value["contract_version"] = "managed-assessment-v1"
    value["assessment_authority"] = "assessment_only"
    value["approval_gate"] = {
        "required": gate.enabled,
        "revision": gate.revision,
        "scope": {
            "workspace_id": str(gate.workspace_id),
            "assessment_key_id": str(gate.assessment_key_id),
        },
    }
    if isinstance(value.get("policy_version"), str):
        value["policy_version"] += f":managed:{gate.revision}:{gate.enabled}"
    if gate.enabled:
        value["decision"] = "UNAVAILABLE"
        value["signal_score"] = None
        value["blocking_reasons"] = ["human_approval_required"]
        value["remediation"] = [
            "An operator must turn off the managed review lock before new entries can be assessed."
        ]
        # Keep the unavailable-attempt id; it is not a synthetic assessment score.
    return value
