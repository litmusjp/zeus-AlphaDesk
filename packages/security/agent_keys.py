from __future__ import annotations

import hashlib
import secrets

KEY_PREFIX = "adk_"


def generate_agent_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_agent_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def key_prefix(secret: str) -> str:
    return secret[:12]
