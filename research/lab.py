"""Shared entry point: load the panel and benchmarks once, then sweep or run tests.

Example:
    import sys; sys.path.insert(0, "research")
    from lab import load_all
    from engine import run_test, sweep
    panel, bench = load_all()
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmarks  # noqa: E402
import panel as panel_module  # noqa: E402


def load_all(rebuild: bool = False):
    return panel_module.load(rebuild=rebuild), benchmarks.load()
