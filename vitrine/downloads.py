"""Background game installs with progress reporting.

GUI-free so it can run from any thread. A :class:`DownloadJob` wraps a shell
invocation (legendary install, GOG installer download, …) and parses percentage
output from its stderr/stdout, invoking callbacks as it goes. Consumers marshal
the callbacks back onto the main loop.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

#: Matches a percentage token in streamed output (legendary/GOG installers).
_PERCENT_RE = re.compile(r"(\d{1,3})%")

#: Ordered combos legendary installs; a job is "done" only after all finish.
#: (Parsed from output; not needed for state tracking.)
_on_progress = Callable[[float], None]  # 0.0..1.0 (or a negative sentinel)
_on_done = Callable[[int], None]  # return code
_on_line = Callable[[str], None]  # raw output line (optional)


class DownloadJob:
    """Run a command and stream its output through a progress callback."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        progress: _on_progress | None = None,
        done: _on_done | None = None,
    ) -> None:
        self.command = list(command)
        self.env = env
        self.progress = progress
        self.done = done
        self._process: subprocess.Popen | None = None
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="vitrine-download")
        self._thread.start()

    def stop(self) -> None:
        process = self._process
        if process is not None:
            process.terminate()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def wait(self, timeout: float | None = None) -> int | None:
        self._finished.wait(timeout)
        return self._process.poll() if self._process is not None else None

    # -- internals -----------------------------------------------------------

    def _run(self) -> None:
        logger.info("Download job: %s", " ".join(map(str, self.command)))
        result = None
        try:
            process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=self.env,
            )
        except OSError as exc:
            logger.error("Could not start download %s: %s", self.command[0], exc)
            self._finished.set()
            if self.done is not None:
                self.done(-1)
            return

        self._process = process
        assert process.stdout is not None
        try:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                percent = _extract_percent(line)
                if percent is not None and self.progress is not None:
                    self.progress(percent)
        finally:
            process.wait()
            self._finished.set()
            result = process.returncode
            logger.info("Download job finished rc=%s", result)
            if self.done is not None:
                self.done(result)


def _extract_percent(line: str) -> float | None:
    """Return the last percentage (0..1) in ``line``, else ``None``."""
    matches = _PERCENT_RE.findall(line)
    if not matches:
        return None
    value = int(matches[-1])
    return max(0.0, min(value / 100.0, 1.0))


def run_download(
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    progress: _on_progress | None = None,
    done: _on_done | None = None,
) -> DownloadJob:
    """Convenience: build and start a :class:`DownloadJob`."""
    job = DownloadJob(command, env=env, progress=progress, done=done)
    job.start()
    return job