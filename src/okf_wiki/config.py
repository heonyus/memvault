"""Path configuration for okf-wiki.

Resolution order for the wiki/bundle root and the home root:
1. CLI flag (``--wiki`` / ``--home``) passed to each command.
2. Environment variables ``OKF_WIKI`` / ``OKF_HOME``.
3. Defaults: ``~/llm-wiki`` and ``~``.
"""

from __future__ import annotations

import os
from pathlib import Path


def wiki_root() -> Path:
    return Path(os.environ.get("OKF_WIKI") or (Path.home() / "llm-wiki")).expanduser()


def home_root() -> Path:
    return Path(os.environ.get("OKF_HOME") or Path.home()).expanduser()
