"""Minimal parser for Steam's text VDF format.

VDF (Valve Data Format) is the ``"key"  "value"`` / ``"key" { ... }`` notation
used by ``libraryfolders.vdf`` and the ``appmanifest_<appid>.acf`` files. Only
what Vitrine needs is implemented: nested dictionaries with string leaves.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_OPENER = re.compile(r'("(?:[^"\\]|\\.)*"\s*\{)|("(?:[^"\\]|\\.)*"\s*"(?:[^"\\]|\\.)*")')
_TOKEN = re.compile(r"(\{|\})|(\"(?:[^\"\\]|\\.)*\")")


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse VDF text into nested ``dict[str, str | dict]``. Leaves are strings."""
    tokens: list[str] = []
    for match in _TOKEN.finditer(text):
        tokens.append(match.group(0))
    if not tokens:
        return {}

    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    pending: str | None = None
    i = 0

    while i < len(tokens):
        token = tokens[i]
        if token in ("{", "}"):
            if pending is not None:
                # A block following a bare key: start the section under it.
                section: dict[str, Any] = {}
                stack[-1][pending] = section
                stack.append(section)
                pending = None
            elif token == "{":
                raise ValueError("Unexpected '{' in VDF")
            else:
                if len(stack) > 1:
                    stack.pop()
        else:
            word = _unescape(token.strip('"'))
            if pending is not None:
                stack[-1][pending] = word
                pending = None
            else:
                pending = word
        i += 1

    return root


def _unescape(value: str) -> str:
    return (
        value.replace(r"\\", "\x00")
        .replace(r"\"", '"')
        .replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace("\x00", "\\")
    )


def parse_vdf_file(path: str | Path) -> dict[str, Any]:
    """Read and parse a VDF file, returning its root dictionary."""
    return parse_vdf(Path(path).read_text(encoding="utf-8", errors="replace"))