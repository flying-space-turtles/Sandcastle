from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets


PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = 200_000
PBKDF2_SALT_BYTES = 16
PBKDF2_DIGEST_BYTES = 32
TOKEN_HASH_PREFIX = "pbkdf2_sha256"


def hash_team_token(token: str) -> str:
    """Return a salted, slow hash suitable for persisted team API tokens."""
    if not token:
        raise ValueError("team token must be non-empty")
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        token.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=PBKDF2_DIGEST_BYTES,
    )
    return "$".join(
        (
            TOKEN_HASH_PREFIX,
            str(PBKDF2_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_team_token(token: str, encoded_hash: str) -> bool:
    """Verify a team API token without exposing parse failures to callers."""
    if not token or not encoded_hash:
        return False
    try:
        prefix, iterations_raw, salt_raw, expected_raw = encoded_hash.split("$", 3)
        if prefix != TOKEN_HASH_PREFIX:
            return False
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_raw.encode("ascii"), altchars=b"-_", validate=True)
        expected = base64.b64decode(
            expected_raw.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        if not 100_000 <= iterations <= 1_000_000:
            return False
        if len(salt) < PBKDF2_SALT_BYTES or len(expected) != PBKDF2_DIGEST_BYTES:
            return False
    except (binascii.Error, TypeError, ValueError):
        return False

    try:
        actual = hashlib.pbkdf2_hmac(
            PBKDF2_ALGORITHM,
            token.encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected),
        )
    except (OverflowError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)
