from __future__ import annotations

from importlib.metadata import requires
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest


class RuntimePackagingTests(unittest.TestCase):
    def test_shared_runtime_is_declared_in_project_dependencies(self):
        manifest = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        )
        self.assertTrue(any(
            requirement.startswith("kwikledgers-fastmcp")
            for requirement in manifest["project"]["dependencies"]
        ))

    def test_installed_metadata_requires_shared_runtime(self):
        self.assertTrue(any(
            requirement.startswith("kwikledgers-fastmcp")
            for requirement in requires("kwikledgers-langgraph") or []
        ))

    def test_client_imports_do_not_load_server(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                "import kwikledgers_langgraph.cli; "
                "import kwikledgers_langgraph.request_metrics_hook; "
                "import kwikledgers_langgraph.handoff_cli; "
                "assert 'fastmcp' not in sys.modules; "
                "assert 'kwikledgers_fastmcp.server' not in sys.modules",
            ],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
