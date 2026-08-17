"""`_env_float`/`_env_int`'s failure mode on a malformed environment value.

A bare `float(raw)`/`int(raw)` failure names neither the environment variable
nor the value that broke it — just "could not convert string to float:
'xyz'", which is fine in a REPL and useless in a deploy log next to forty
other environment variables. These two are the only place in `arie.config`
that parse a string from the environment, so this is the one seam a broken
value can be made to fail clearly at.
"""

from __future__ import annotations

import pytest

from arie.config import _env_float, _env_int


def test_env_float_returns_the_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIE_TEST_FLOAT", raising=False)
    assert _env_float("ARIE_TEST_FLOAT", 1.5) == 1.5


def test_env_float_parses_a_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIE_TEST_FLOAT", "2.75")
    assert _env_float("ARIE_TEST_FLOAT", 1.5) == 2.75


def test_env_float_names_the_variable_and_value_on_malformed_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIE_TEST_FLOAT", "not-a-number")
    with pytest.raises(ValueError, match=r"ARIE_TEST_FLOAT.*not-a-number"):
        _env_float("ARIE_TEST_FLOAT", 1.5)


def test_env_int_returns_the_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIE_TEST_INT", raising=False)
    assert _env_int("ARIE_TEST_INT", 3) == 3


def test_env_int_parses_a_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIE_TEST_INT", "7")
    assert _env_int("ARIE_TEST_INT", 3) == 7


def test_env_int_names_the_variable_and_value_on_malformed_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIE_TEST_INT", "3.5")  # a float string is not an int
    with pytest.raises(ValueError, match=r"ARIE_TEST_INT.*3\.5"):
        _env_int("ARIE_TEST_INT", 3)
