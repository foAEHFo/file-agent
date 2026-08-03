from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_refresh_feedback_covers_success_and_failure() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = Path(__file__).with_name("verify_refresh_feedback.js")
    subprocess.run([node, script], check=True)
