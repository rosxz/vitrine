"""Games added by hand: native Linux binaries, or Windows games the user points us at."""

from __future__ import annotations

from pathlib import Path

from ..library import Library
from ..util import expand
from .base import Source, registry


class LocalSource(Source):
    """The 'local' source. Nothing is fetched; games are added manually."""

    id = "local"
    name = "Local games"
    icon = "drive-harddisk-symbolic"

    def __init__(self, library: Library) -> None:
        self.library = library

    def sync(self) -> int:
        return len(self.library.games(source=self.id))

    def sync_installed(self) -> int:
        """A local game counts as installed while its executable still exists."""
        installed_count = 0
        for game in self.library.games(source=self.id):
            installed = self._executable_exists(game.executable)
            if installed != game.installed:
                game.installed = installed
                self.library.update(game)
            installed_count += int(installed)
        return installed_count

    @staticmethod
    def _executable_exists(executable: str | None) -> bool:
        if not executable:
            return False
        resolved = expand(executable)
        return bool(resolved) and Path(resolved).is_file()


registry.register(LocalSource)
