"""Theme registry and persistence (no GTK display required)."""

from __future__ import annotations

import pytest

from vitrine.ui.theme import (
    DEFAULT_THEME,
    THEMES,
    is_theme,
    resolve_theme,
    theme_ids,
)


def test_default_is_galaxy() -> None:
    assert DEFAULT_THEME == "galaxy"


def test_registry_contains_expected_themes() -> None:
    assert set(theme_ids()) >= {"galaxy", "system"}


def test_full_theme_metadata() -> None:
    galaxy = THEMES["galaxy"]
    assert galaxy.id == "galaxy"
    assert galaxy.name
    assert galaxy.description
    system = THEMES["system"]
    assert system.name == "Follow system"


def test_is_theme() -> None:
    assert is_theme("galaxy")
    assert is_theme("system")
    assert not is_theme("bogus")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("galaxy", "galaxy"),
        ("system", "system"),
        ("bogus", DEFAULT_THEME),
        (None, DEFAULT_THEME),
        ("", DEFAULT_THEME),
        (42, DEFAULT_THEME),
    ],
)
def test_resolve_theme(value, expected: str) -> None:
    assert resolve_theme(value) == expected