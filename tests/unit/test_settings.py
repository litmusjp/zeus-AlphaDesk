from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.configuration.settings import Settings, SystemMode


def test_default_mode_is_paper_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.mode is SystemMode.PAPER_ONLY


@pytest.mark.parametrize("unsafe_mode", ["LIVE", "live", "PAPER", "SANDBOX"])
def test_every_non_paper_mode_fails_closed(unsafe_mode: str) -> None:
    with pytest.raises(ValidationError, match="live trading fails closed"):
        Settings(mode=unsafe_mode, _env_file=None)


def test_secret_values_are_masked() -> None:
    settings = Settings(
        supabase_secret_key="sb_secret_test-only-value",
        credential_master_keys="v1:cGFwZXIta2V5LXRoYXQtaXMtZXhhY3RseS0zMmI",
        _env_file=None,
    )
    assert "cGFwZXI" not in repr(settings)
    assert "sb_secret_test-only-value" not in repr(settings)


def test_global_operator_credentials_are_ignored() -> None:
    settings = Settings(
        alpaca_api_key_id="legacy-paper-key",
        alpaca_api_secret_key="legacy-paper-secret",
        openai_api_key="legacy-openai-key",
        _env_file=None,
    )
    assert not hasattr(settings, "alpaca_api_key_id")
    assert not hasattr(settings, "openai_api_key")


def test_standalone_minimum_signal_score_override_is_optional_and_bounded() -> None:
    from decimal import Decimal

    assert Settings(_env_file=None).standalone_minimum_signal_score is None
    assert Settings(
        _env_file=None, standalone_minimum_signal_score="25"
    ).standalone_minimum_signal_score == Decimal("25")
    for value in ("-0.1", "100.1", "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, standalone_minimum_signal_score=value)


def test_global_ai_provider_must_remain_fixture() -> None:
    assert Settings(_env_file=None, llm_provider="fixture").llm_provider == "fixture"
    with pytest.raises(ValidationError, match="connected providers are BYOK"):
        Settings(_env_file=None, llm_provider="openai")


def test_supabase_secret_must_be_modern_server_only_key() -> None:
    with pytest.raises(ValidationError, match="modern server-only"):
        Settings(_env_file=None, supabase_secret_key="legacy-service-role-jwt")
