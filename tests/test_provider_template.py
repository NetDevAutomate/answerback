"""The provider template page builds a package that passes all six conformance cases.

T-277 review 3: the page's complete declaration must run every case, `subject` included, not
skip one. This test writes the page's own code blocks out as the `answerback-example`
package, registers it the way an install does (a `.dist-info` with its entry point), and runs
the page's conformance test in a fresh interpreter.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs" / "PROVIDER-TEMPLATE.md"
# Each package file and a string only its code block on the page contains.
FILES = {
    "src/answerback_example/config.py": "class ExampleConfig(ProviderConfig):",
    "src/answerback_example/adapter.py": "class ExampleAdapter(StoredAdapter):",
    "src/answerback_example/__init__.py": "def example_plugin() -> ProviderPluginSpec:",
    "tests/conftest.py": "# tests/conftest.py\n",
    "tests/test_provider_conformance.py": "# tests/test_provider_conformance.py\n",
}


def python_blocks() -> list[str]:
    return re.findall(r"^```python\n(.*?)^```$", TEMPLATE.read_text(), re.MULTILINE | re.DOTALL)


def test_the_template_package_passes_all_six_conformance_cases(tmp_path: Path) -> None:
    blocks = python_blocks()
    for relative, marker in FILES.items():
        (source,) = [block for block in blocks if marker in block]
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    info = tmp_path / "src" / "answerback_example-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: answerback-example\nVersion: 0.1.0\n"
    )
    (info / "entry_points.txt").write_text(
        "[answerback.providers]\nexample = answerback_example:example_plugin\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs"]
        + ["tests/test_provider_conformance.py"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert re.search(r"^6 passed\b", run.stdout.splitlines()[-1]), run.stdout
