"""Tests for the umu-run (unified launcher) wrapper."""

from __future__ import annotations

import pytest

from vitrine.infra.wine import umu


def test_umu_binary_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(umu.UMU_ENV, "/opt/umu-run")
    assert umu.umu_binary() == "/opt/umu-run"
    assert umu.is_available()


def test_umu_binary_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(umu.UMU_ENV, raising=False)
    monkeypatch.setattr(umu.shutil, "which", lambda _n: None)
    assert not umu.is_available()
    with pytest.raises(umu.UmuError):
        umu.umu_binary()


def test_umu_env_is_clean_and_sets_protocol_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pollute like the app might; umu_env must NOT leak LD_LIBRARY_PATH.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/nix/store/gtk/lib")
    env = umu.umu_env(
        "/home/u/.local/share/vitrine/prefixes/game",
        proton_path="/proton",
        game_id="abc",
        install_path="/games/slug",
    )
    assert env["GAMEID"] == "abc"
    assert env["WINEPREFIX"] == "/home/u/.local/share/vitrine/prefixes/game"
    assert env["PROTONPATH"] == "/proton"
    assert env["WINEARCH"] == "win64"
    assert "LD_LIBRARY_PATH" not in env
    assert "STEAM_RUNTIME_LIBRARY_PATH" not in env
    assert "/usr/bin:/bin" in env["PATH"]


def test_umu_env_never_leaks_vk_icd_filenames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pointing the Vulkan loader at the Nix mesa ICD that pressure-vessel
    doesn't stage makes DXVK fail to init and the game exit without a window.
    VK_ICD_FILENAMES must never reach the umu/Proton env, even if the parent
    process exports it."""

    monkeypatch.setenv("VK_ICD_FILENAMES", "/nix/store/mesa/share/vulkan/icd.d/intel_icd.x86_64.json")
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc", install_path="/g")
    assert "VK_ICD_FILENAMES" not in env


def test_umu_env_always_sets_game_and_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whatever the caller passes, GAMEID and PROTON_VERB are fixed.
    monkeypatch.delenv("GAMEID", raising=False)
    env = umu.umu_env("/p", proton_path="/proton", game_id="other")
    assert env["GAMEID"] == "other"
    assert env["PROTON_VERB"] == "waitforexitandrun"


def test_umu_env_extra_does_not_override_core(monkeypatch: pytest.MonkeyPatch) -> None:
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc", extra={"GAMEID": "evil", "FOO": "1"})
    assert env["GAMEID"] == "abc"  # core wins
    assert env["FOO"] == "1"


def test_umu_command_wraps_in_steam_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(umu.UMU_ENV, "/opt/umu-run")

    def _which(name):
        return "/run/current-system/sw/bin/steam-run" if name == "steam-run" else None

    monkeypatch.setattr(umu.shutil, "which", _which)
    cmd = umu.umu_command("/games/Game.exe", ["--fullscreen"])
    assert cmd == ["steam-run", "/opt/umu-run", "/games/Game.exe", "--fullscreen"]


def test_umu_command_without_fhs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(umu.UMU_ENV, "/opt/umu-run")
    cmd = umu.umu_command("/games/Game.exe", fhs=False)
    assert cmd == ["/opt/umu-run", "/games/Game.exe"]

def test_umu_env_discovers_xauthority_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """GUI-launched Vitrine may not export XAUTHORITY; discover it so the X11
    game window can authenticate to Xwayland."""

    auth = tmp_path / "xauth"
    auth.write_text("auth")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(umu, "_discover_xauthority", lambda: str(auth))
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc")
    assert env["XAUTHORITY"] == str(auth)


def test_umu_env_keeps_existing_xauthority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/custom/auth")

    def _discover():
        return "/should/not/override"

    monkeypatch.setattr(umu, "_discover_xauthority", _discover)
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc")
    assert env["XAUTHORITY"] == "/custom/auth"


def test_umu_env_no_display_does_not_add_xauthority(monkeypatch: pytest.MonkeyPatch) -> None:
    # No DISPLAY and no XAUTHORITY in env: nothing should be synthesized.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(umu, "_discover_xauthority", lambda: "/x")
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc", install_path="/g")
    assert "XAUTHORITY" not in env


def test_discover_xauthority_finds_mutter_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    auth = tmp_path / ".mutter-Xwaylandauth.ABC"
    auth.write_text("auth")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert umu._discover_xauthority() == str(auth)


def test_flatpak_icd_rewrites_misdeclared_gl32_library_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Inside the Flatpak the GL32 extension's ICD JSON points at
    .../GL/default/lib/... but the drivers mount at .../GL/lib/... . The helper
    must rewrite the JSON to the real driver and publish it to the loader's
    default search dir, so 32-bit DXVK can create a Vulkan instance."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".var/app/io.github.rosxz.vitrine/data"))
    monkeypatch.setenv("HOME", str(home))

    gl = tmp_path / "gl"
    icd_dir = gl / "lib" / "vulkan" / "icd.d"
    icd_dir.mkdir(parents=True)
    (gl / "lib" / "libvulkan_intel.so").write_bytes(b"ELF-driver")
    real = json.dumps(
        {
            "ICD": {
                "api_version": "1.4.354",
                "library_arch": "32",
                "library_path": "/app/lib/i386-linux-gnu/GL/default/lib/libvulkan_intel.so",
            },
            "file_format_version": "1.0.1",
        }
    )
    (icd_dir / "intel_icd.i686.json").write_text(real)

    monkeypatch.setattr(umu, "_GL32_DIRS", (str(gl),))
    env: dict[str, str] = {}
    umu._fix_flatpak_32bit_icd(env)

    out = home / ".local" / "share" / "vulkan" / "icd.d" / "intel_icd.i686.json"
    assert out.exists()
    fixed = json.loads(out.read_text())
    assert fixed["ICD"]["library_path"] == str(gl / "lib" / "libvulkan_intel.so")


def test_flatpak_icd_skipped_outside_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """No-op when not running under a Flatpak (native NixOS runs): nothing is
    written and no VK_ICD_FILENAMES is injected, keeping NixOS behaviour."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("HOME", str(home))

    gl = tmp_path / "gl"
    icd_dir = gl / "lib" / "vulkan" / "icd.d"
    icd_dir.mkdir(parents=True)
    (gl / "lib" / "libvulkan_intel.so").write_bytes(b"ELF-driver")
    (icd_dir / "intel_icd.i686.json").write_text(
        json.dumps(
            {
                "ICD": {
                    "api_version": "1.4.354",
                    "library_arch": "32",
                    "library_path": "/app/lib/i386-linux-gnu/GL/default/lib/libvulkan_intel.so",
                }
            }
        )
    )

    monkeypatch.setattr(umu, "_GL32_DIRS", (str(gl),))
    env: dict[str, str] = {}
    umu._fix_flatpak_32bit_icd(env)
    # Not a flatpak: helper must be inert.
    assert not (home / ".local" / "share" / "vulkan").exists()
    assert "VK_ICD_FILENAMES" not in env
