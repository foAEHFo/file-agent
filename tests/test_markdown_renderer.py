from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_markdown_renderer_builds_safe_structured_dom() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = Path(__file__).with_name("verify_markdown_renderer.js")
    subprocess.run([node, script], check=True)
