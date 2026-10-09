"""TradeTest's browser modules, through their node test suites in tests/js. Skipped where node is not installed.

Node 21 and later read `node --test` arguments as file patterns, so the suites are named by a glob, which node
expands itself; `node --test tests/js` would look for a file called tests/js.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_node_suites_pass():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    assert list((ROOT / "tests" / "js").glob("*.test.mjs")), "no node suites in tests/js"
    result = subprocess.run([node, "--test", "tests/js/*.test.mjs"], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300)
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"
