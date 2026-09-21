"""Tests for the background download manager and progress parsing."""

from __future__ import annotations

import sys
from pathlib import Path

from vitrine.downloads import DownloadJob, _extract_percent, run_download


def test_extract_percent() -> None:
    assert _extract_percent("[   0%] - [   0.0 / 100.0 MiB]") == 0.0
    assert _extract_percent("[  52%] - [ 52 / 100 MiB]") == 0.52
    assert _extract_percent("Download finished") is None
    assert _extract_percent("100%") == 1.0
    # last percentage wins (e.g. per-file then overall)
    assert _extract_percent("[ 10%] overall 99%") == 0.99


def test_job_reports_progress(tmp_path: Path) -> None:
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys,time\n"
        "for p in (0, 25, 50, 100):\n"
        "    print(f'[{p}%]')\n"
        "    sys.stdout.flush()\n"
    )
    progress: list[float] = []
    done: list[int] = []
    job = DownloadJob(
        [sys.executable, str(script)],
        progress=progress.append,
        done=done.append,
    )
    job.wait(timeout=2)  # not yet started; returns early
    job.start()
    job.wait(timeout=10)
    assert done and done[0] == 0
    assert progress == [0.0, 0.25, 0.5, 1.0]


def test_job_failure_reports_returncode(tmp_path: Path) -> None:
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(3)\n")
    done: list[int] = []
    job = DownloadJob([sys.executable, str(script)], done=done.append)
    job.start()
    job.wait(timeout=10)
    assert done == [3]


def test_run_download_starts_and_finishes(tmp_path: Path) -> None:
    script = tmp_path / "ok.py"
    script.write_text("print('x')\n")
    done: list[int] = []
    job = run_download([sys.executable, str(script)], done=done.append)
    job.wait(timeout=10)
    assert done == [0]
    assert not job.is_running


def test_job_captures_output_lines(tmp_path: Path) -> None:
    script = tmp_path / "lines.py"
    script.write_text("import sys\nprint('line one')\nprint('line two')\n")
    lines: list[str] = []
    done: list[int] = []
    job = DownloadJob([sys.executable, str(script)], on_line=lines.append, done=done.append)
    job.start()
    job.wait(timeout=10)
    assert done == [0]
    assert any("line one" in line for line in lines)
    assert any("line two" in line for line in lines)
    # The accumulated buffer mirrors what the callback stream received.
    assert len(job.line_buffer) == len(lines)
    assert job.line_buffer == lines