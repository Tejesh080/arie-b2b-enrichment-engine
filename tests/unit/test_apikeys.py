"""Unit tests for `arie.apikeys` generation/hashing — pure functions, no
database. The create/list/revoke/verify persistence functions need a real
`organization_api_keys` table to mean anything and are covered against a
live database in `tests/integration/test_api_keys_integration.py` instead.
"""

from __future__ import annotations

from arie.apikeys import API_KEY_PREFIX, generate_api_key, looks_like_api_key


def test_generated_key_carries_the_arie_prefix() -> None:
    generated = generate_api_key()
    assert generated.raw_key.startswith(API_KEY_PREFIX)
    assert generated.key_prefix.startswith(API_KEY_PREFIX)


def test_key_prefix_is_a_leading_substring_of_the_raw_key() -> None:
    generated = generate_api_key()
    assert generated.raw_key.startswith(generated.key_prefix)
    assert len(generated.key_prefix) < len(generated.raw_key)


def test_two_generated_keys_are_never_equal() -> None:
    """Not a statistical claim — `secrets.token_urlsafe(32)` is 256 bits of
    entropy, so this failing would mean the RNG is broken, not unlucky."""
    a = generate_api_key()
    b = generate_api_key()
    assert a.raw_key != b.raw_key
    assert a.key_hash != b.key_hash


def test_key_hash_is_deterministic_for_the_same_raw_key() -> None:
    """`verify_api_key` re-derives the hash from a presented raw key and
    compares — this is the property that makes that comparison meaningful."""
    from arie.apikeys import _hash  # internal, tested directly on purpose

    generated = generate_api_key()
    assert _hash(generated.raw_key) == generated.key_hash


def test_key_hash_never_contains_the_raw_key() -> None:
    generated = generate_api_key()
    assert generated.raw_key not in generated.key_hash


def test_looks_like_api_key_distinguishes_from_a_jwt() -> None:
    generated = generate_api_key()
    assert looks_like_api_key(generated.raw_key) is True
    # A JWT is three dot-separated base64url segments — never starts with "arie_".
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.dGVzdHNpZw"
    assert looks_like_api_key(fake_jwt) is False
