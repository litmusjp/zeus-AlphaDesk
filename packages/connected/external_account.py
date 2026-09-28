from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ExternalAccountBinding:
    workspace_id: UUID
    key_id: UUID
    account_id: str
    sandbox_id: str
    environment: str
    access_token: str = field(repr=False)


@dataclass(frozen=True)
class ExternalAccountEvidence:
    account_id: str
    sandbox_id: str
    environment: str
    equity: Decimal
    fetched_at: datetime


class ExternalAccountProvider(Protocol):
    async def get_account(
        self,
        *,
        workspace_id: UUID,
        key_id: UUID,
        account_id: str,
        sandbox_id: str,
        environment: str,
    ) -> ExternalAccountEvidence | None: ...


class AlpacaExternalAccountProvider:
    """Optional read-only OAuth provider; bindings are server-side only."""

    endpoint = "https://paper-api.alpaca.markets/v2/account"

    def __init__(self, bindings: tuple[ExternalAccountBinding, ...]) -> None:
        key_ids: set[UUID] = set()
        for binding in bindings:
            if binding.key_id in key_ids:
                raise ValueError("external account bindings configuration is invalid")
            key_ids.add(binding.key_id)
        self._bindings = bindings

    @classmethod
    def from_json(cls, raw: str) -> AlpacaExternalAccountProvider:
        """Parse server-only bindings; callers must not pass request data here."""
        parsed = json.loads(raw)
        records = parsed.get("bindings") if isinstance(parsed, dict) else parsed
        if not isinstance(records, list) or not records:
            raise ValueError("external account bindings configuration is invalid")
        bindings: list[ExternalAccountBinding] = []
        seen_keys: set[UUID] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("external account bindings configuration is invalid")
            try:
                binding = ExternalAccountBinding(
                    workspace_id=UUID(_required_string(record, "workspace_id")),
                    key_id=UUID(_required_string(record, "key_id")),
                    account_id=_required_string(record, "account_id"),
                    sandbox_id=_required_string(record, "sandbox_id"),
                    environment=_required_string(record, "environment"),
                    access_token=_required_string(record, "access_token"),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("external account bindings configuration is invalid") from error
            if binding.environment != "PAPER" or binding.key_id in seen_keys:
                raise ValueError("external account bindings configuration is invalid")
            seen_keys.add(binding.key_id)
            bindings.append(binding)
        return cls(tuple(bindings))

    async def get_account(
        self,
        *,
        workspace_id: UUID,
        key_id: UUID,
        account_id: str,
        sandbox_id: str,
        environment: str,
    ) -> ExternalAccountEvidence | None:
        if environment != "PAPER":
            return None
        binding = next(
            (
                item
                for item in self._bindings
                if item.workspace_id == workspace_id
                and item.key_id == key_id
                and item.account_id == account_id
                and item.sandbox_id == sandbox_id
                and item.environment == "PAPER"
                and item.access_token
            ),
            None,
        )
        if binding is None:
            return None
        try:
            body = await asyncio.to_thread(self._get, binding.access_token)
            returned_id = body["id"]
            status = body["status"]
            equity = Decimal(str(body["equity"]))
            blocked = any(
                field not in body or type(body[field]) is not bool or body[field]
                for field in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
            )
        except (KeyError, InvalidOperation, TypeError, ValueError, OSError, TimeoutError):
            return None
        if (
            type(returned_id) is not str
            or type(status) is not str
            or returned_id != account_id
            or status != "ACTIVE"
            or blocked
            or not equity.is_finite()
            or equity <= 0
        ):
            return None
        return ExternalAccountEvidence(
            account_id=returned_id,
            sandbox_id=binding.sandbox_id,
            environment="PAPER",
            equity=equity,
            fetched_at=datetime.now(UTC),
        )

    @classmethod
    def _get(cls, token: str) -> dict[str, object]:
        request = urllib.request.Request(
            cls.endpoint,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with _open_without_redirects(request) as response:
            value = json.load(response)
        if not isinstance(value, dict):
            raise ValueError("Alpaca account response is not an object")
        return value


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        raise OSError("redirects are not allowed for bearer requests")


def _required_string(record: dict[str, Any], name: str) -> str:
    value = record[name]
    if type(value) is not str or not value:
        raise ValueError("external account bindings configuration is invalid")
    return value


def _open_without_redirects(request: urllib.request.Request) -> Any:
    return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=10)
