"""Tests for the GOG comet (Galaxy communication service) bridge."""

from __future__ import annotations

import pytest

from vitrine.sources.gog import comet


def test_comet_binary_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(comet.COMET_ENV, "/opt/comet")
    assert comet.comet_binary() == "/opt/comet"
    assert comet.is_installed()


def test_comet_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(comet.COMET_ENV, raising=False)
    monkeypatch.setattr(comet.shutil, "which", lambda _name: None)
    assert not comet.is_installed()
    with pytest.raises(comet.CometError):
        comet.comet_binary()


def test_comet_args_include_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(comet.COMET_ENV, "/opt/comet")
    args = comet.comet_args("ACCESS", "REFRESH", "u42", "player")
    assert args[0] == "/opt/comet"
    # Values use --opt=value so a value beginning with '-' can't be read as a flag.
    assert "--access-token=ACCESS" in args
    assert "--refresh-token=REFRESH" in args
    assert "--user-id=u42" in args
    assert "--username=player" in args
    assert comet._QUIT_FLAG in args


def test_comet_args_binds_dashed_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """A username/value starting with '-' must not be parsed as a flag."""
    monkeypatch.setenv(comet.COMET_ENV, "/opt/comet")
    args = comet.comet_args("tok", "rtok", "1", "-zname")
    assert "--username=-zname" in args


def test_write_wrapper_generates_tokened_script(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(comet.COMET_ENV, "/opt/comet")
    dest = tmp_path / "g.sh"
    comet.write_wrapper(
        str(dest),
        access_token="ACCESS",
        refresh_token="REFRESH",
        user_id="u42",
        username="player",
    )
    text = dest.read_text()
    assert "ACCESS" in text
    assert "REFRESH" in text
    assert "u42" in text
    assert "player" in text
    assert "exec \"$@\"" in text
    # Must be owner-readable (embeds tokens) AND executable.
    assert (dest.stat().st_mode & 0o777) == 0o700