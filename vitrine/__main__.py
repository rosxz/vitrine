"""Entry point: ``python -m vitrine``."""

from __future__ import annotations

import sys

from .application import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
