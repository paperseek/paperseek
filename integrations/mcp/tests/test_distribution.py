from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest


MCP_ROOT = Path(__file__).resolve().parents[1]
COMMUNITY_ROOT = MCP_ROOT.parents[1]
VERSION = "1.0.0b4"
RELEASE = "1.0.0-beta4"
SERVER_NAME = "io.github.paperseek/paperseek"


class M4MCPDistributionContract(unittest.TestCase):
    def test_independent_distribution_has_exact_main_wheel_and_sdk_dependencies(self):
        configuration = tomllib.loads(
            (MCP_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = configuration["project"]

        self.assertEqual(project["name"], "paperseek-mcp")
        self.assertEqual(project["version"], VERSION)
        self.assertEqual(project["scripts"], {"paperseek-mcp": "paperseek_mcp.cli:main"})
        self.assertIn(f"paperseek=={VERSION}", project["dependencies"])
        self.assertIn("mcp==2.0.0", project["dependencies"])
        self.assertEqual(
            configuration["tool"]["setuptools"]["data-files"],
            {"share/paperseek-mcp": ["server.json"]},
        )
        self.assertNotIn("paperseek-mcp", (COMMUNITY_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))

    def test_registry_metadata_identifies_the_pypi_and_oci_artifacts(self):
        metadata = json.loads((MCP_ROOT / "server.json").read_text(encoding="utf-8"))

        self.assertEqual(metadata["$schema"], "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json")
        self.assertEqual(metadata["name"], SERVER_NAME)
        self.assertEqual(metadata["version"], RELEASE)
        self.assertEqual(metadata["repository"], {
            "source": "github",
            "url": "https://github.com/paperseek/paperseek",
        })
        packages = {item["registryType"]: item for item in metadata["packages"]}
        self.assertEqual(packages["pypi"]["identifier"], "paperseek-mcp")
        self.assertEqual(packages["pypi"]["version"], VERSION)
        self.assertEqual(packages["pypi"]["transport"], {"type": "stdio"})
        self.assertEqual(
            packages["oci"]["identifier"],
            f"ghcr.io/paperseek/paperseek-mcp:{RELEASE}",
        )
        self.assertEqual(packages["oci"]["transport"], {
            "type": "streamable-http",
            "url": "http://127.0.0.1:8766/mcp",
        })
        self.assertNotIn("remotes", metadata)

    def test_readme_contains_registry_ownership_marker(self):
        readme = (MCP_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(f"<!-- mcp-name: {SERVER_NAME} -->", readme)

if __name__ == "__main__":
    unittest.main()
