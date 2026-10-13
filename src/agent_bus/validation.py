from __future__ import annotations

import re


class AuthenticationError(ValueError):
    """Missing, invalid or unusable identity/session data."""


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise AuthenticationError(
            "Invalid identity: use 1-128 letters, digits, dots, underscores or hyphens"
        )
    return value
