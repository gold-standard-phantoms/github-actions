"""Smoke test for the fixture package."""

from gsp_ci_fixture import add


def test_add() -> None:
    assert add(2, 3) == 5
