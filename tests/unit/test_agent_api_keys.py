from packages.security.agent_keys import generate_agent_key, hash_agent_key, key_prefix


def test_agent_key_is_high_entropy_and_only_hash_is_persisted() -> None:
    secret = generate_agent_key()
    assert secret.startswith("adk_")
    assert len(secret) >= 40
    assert hash_agent_key(secret) != secret
    assert key_prefix(secret).startswith("adk_")
