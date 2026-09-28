"""The Runner domain value object (pure; no discovery/I-O)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runner:
    id: str
    name: str
    path: str  # absolute path to the wine binary (or "" for presets)
    kind: str = "wine"  # "wine" | "proton"

    @property
    def is_preset(self) -> bool:
        return not self.path

    @property
    def is_proton(self) -> bool:
        """Authoritative proton check: the strict dist-dir probe on the binary.

        Sources of truth are reconciled here instead of letting callers each
        guess (the looser substring heuristic in discovery vs the strict
        :func:`vitrine.services.launch._is_proton_path`). Presets are never
        proton.
        """
        if not self.path:
            return False
        from vitrine.services.launch import _is_proton_path

        return _is_proton_path(self.path)

    @property
    def is_native(self) -> bool:
        """A runner is never "native"; native games have no runner at all."""
        return False