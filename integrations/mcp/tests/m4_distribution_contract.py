from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest


MCP_ROOT = Path(__file__).resolve().parents[1]
COMMUNITY_ROOT = MCP_ROOT.parents[1]
VERSION = "1.0.0b3"
RELEASE = "1.0.0-beta3"
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

    def test_independent_distribution_has_a_reproducible_lock(self):
        lock = tomllib.loads((MCP_ROOT / "uv.lock").read_text(encoding="utf-8"))
        packages = {(item["name"], item["version"]) for item in lock["package"]}

        self.assertIn(("paperseek-mcp", VERSION), packages)
        self.assertIn(("paperseek", VERSION), packages)
        self.assertIn(("mcp", "2.0.0"), packages)

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

    def test_mcp_image_is_independent_and_default_compose_stays_single_service(self):
        dockerfile = (MCP_ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (COMMUNITY_ROOT / "compose.yaml").read_text(encoding="utf-8")
        dockerignore = (COMMUNITY_ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("ghcr.io/paperseek/paperseek-mcp", dockerfile)
        self.assertIn(f'io.modelcontextprotocol.server.name="{SERVER_NAME}"', dockerfile)
        self.assertIn(f'org.opencontainers.image.version="{RELEASE}"', dockerfile)
        self.assertIn('ENTRYPOINT ["paperseek-mcp"]', dockerfile)
        self.assertIn('"--host", "0.0.0.0"', dockerfile)
        self.assertNotIn("PAPERSEEK_MCP_SERVICE_TOKEN=", dockerfile)
        self.assertIn("sha256sum --check SHA256SUMS", dockerfile)
        self.assertIn("--no-index --find-links /wheels paperseek-mcp==1.0.0b3", dockerfile)
        self.assertNotIn("paperseek-mcp", compose)
        self.assertIn("backend/.pytest_cache", dockerignore)

    def test_readme_contains_registry_ownership_marker_and_no_old_extra_install(self):
        readme = (MCP_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(f"<!-- mcp-name: {SERVER_NAME} -->", readme)
        self.assertIn("pip install paperseek-mcp==1.0.0b3", readme)
        self.assertNotIn("paperseek[mcp]", readme)
        self.assertNotIn("paperseek.mcp_server", readme)

    def test_replaced_main_package_entry_and_old_behavior_gates_are_deleted(self):
        main_project = tomllib.loads(
            (COMMUNITY_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]

        self.assertFalse((COMMUNITY_ROOT / "pyproject.toml").exists())
        self.assertFalse((COMMUNITY_ROOT / "paperseek" / "mcp_server.py").exists())
        self.assertNotIn("paperseek-mcp", main_project.get("scripts", {}))
        self.assertNotIn("mcp", main_project.get("optional-dependencies", {}))
        self.assertFalse((COMMUNITY_ROOT / "tests" / "mcp_characterization.py").exists())
        self.assertFalse(
            (COMMUNITY_ROOT / "tests" / "test_mcp_characterization_fixtures.py").exists()
        )
        self.assertFalse(
            (COMMUNITY_ROOT / "tests" / "fixtures" / "mcp" / "stdio_characterization.json").exists()
        )

    def test_ci_and_release_use_the_locked_independent_distribution(self):
        ci = (COMMUNITY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        release = (
            COMMUNITY_ROOT / ".github" / "workflows" / "publish-pypi.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("uv lock --check", ci)
        self.assertIn("m4_official_client_integration.py -v", ci)
        self.assertIn("uv lock --check --project integrations/mcp", release)
        self.assertLess(
            release.index("uv build --wheel --out-dir dist backend"),
            release.index("uv build --wheel --out-dir dist integrations/mcp"),
        )


if __name__ == "__main__":
    unittest.main()
