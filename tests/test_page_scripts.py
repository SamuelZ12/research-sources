import shutil
import subprocess
from pathlib import Path


def test_page_script_suite_passes():
    node = shutil.which("node")
    assert node, "install Node.js: the page scripts Ego runs are tested by tests/*.test.mjs"
    suites = sorted(str(p) for p in Path(__file__).parent.glob("*.test.mjs"))
    result = subprocess.run([node, "--test", *suites], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
