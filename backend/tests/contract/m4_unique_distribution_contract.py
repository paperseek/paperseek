from __future__ import annotations

import os
import re
import tomllib
import unittest
from pathlib import Path

COMMUNITY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_PROJECT = COMMUNITY_ROOT / "backend" / "pyproject.toml"
USER_OWNED_TEST = COMMUNITY_ROOT / "tests" / "test_mcp_server.py"
IGNORED_DIRECTORIES = {".git", ".venv", "build", "dist", "node_modules", "__pycache__"}


def _project_name(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data.get("project", {}).get("name", ""))


def _walk_files(root: Path):
    for current, directories, filenames in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if not name.startswith(".")
            and name not in IGNORED_DIRECTORIES
            and not name.endswith(".egg-info")
        ]
        for filename in filenames:
            yield Path(current) / filename


def _active_text_files() -> list[Path]:
    paths: list[Path] = []
    roots = (
        COMMUNITY_ROOT / ".github",
        COMMUNITY_ROOT / "backend",
        COMMUNITY_ROOT / "docs",
        COMMUNITY_ROOT / "integrations",
        COMMUNITY_ROOT / "integrations" / "skills",
    )
    for root in roots:
        if not root.exists():
            continue
        for path in _walk_files(root):
            if path.suffix.lower() in {".md", ".py", ".toml", ".yml", ".yaml", ".json"}:
                paths.append(path)
    paths.extend(
        path
        for path in (
            COMMUNITY_ROOT / "Dockerfile",
            COMMUNITY_ROOT / "compose.yaml",
            COMMUNITY_ROOT / "README.md",
            COMMUNITY_ROOT / "CONTRIBUTING.md",
            COMMUNITY_ROOT / ".env.example",
        )
        if path.is_file()
    )
    return sorted(set(paths))


class M4UniqueDistributionContract(unittest.TestCase):
    def test_backend_is_the_only_paperseek_distribution_definition(self) -> None:
        definitions = [
            path.relative_to(COMMUNITY_ROOT).as_posix()
            for path in _walk_files(COMMUNITY_ROOT)
            if path.name == "pyproject.toml" and _project_name(path) == "paperseek"
        ]

        self.assertEqual(definitions, ["backend/pyproject.toml"])
        self.assertFalse((COMMUNITY_ROOT / "pyproject.toml").exists())
        self.assertFalse((COMMUNITY_ROOT / "requirements.txt").exists())
        self.assertFalse((COMMUNITY_ROOT / "MANIFEST.in").exists())

    def test_legacy_runtime_and_python_tests_are_absent(self) -> None:
        self.assertFalse((COMMUNITY_ROOT / "paperseek").exists())
        self.assertFalse((COMMUNITY_ROOT / "paperseek_core").exists())
        remaining_python_tests = sorted(
            path.relative_to(COMMUNITY_ROOT).as_posix()
            for path in (COMMUNITY_ROOT / "tests").glob("*.py")
        )
        self.assertEqual(remaining_python_tests, ["tests/test_mcp_server.py"])
        self.assertTrue(USER_OWNED_TEST.is_file())

    def test_active_product_paths_have_no_legacy_entry_or_runtime(self) -> None:
        banned = {
            "legacy console entry": re.compile(r"paperseek" + r"\.cli"),
            "legacy agent": re.compile(r"PaperSeek" + r"Agent"),
            "legacy harness import": re.compile(r"paperseek_core" + r"\.harness"),
            "legacy harness extra": re.compile(r"paperseek" + r"\[harness\]"),
            "PydanticAI dependency": re.compile(r"pydantic" + r"-ai", re.IGNORECASE),
            "LangGraph dependency": re.compile(r"lang" + r"graph", re.IGNORECASE),
            "ModelScope-only quota branch": re.compile(r"MODELSCOPE" + r"_QUOTA_HEADERS"),
        }
        violations: list[str] = []
        for path in _active_text_files():
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8-sig")
            for label, pattern in banned.items():
                if pattern.search(text):
                    violations.append(f"{path.relative_to(COMMUNITY_ROOT)}: {label}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_skill_contains_guidance_only(self) -> None:
        self.assertFalse((COMMUNITY_ROOT / "integrations" / "skills" / "paperseek" / "scripts").exists())
        skill = (COMMUNITY_ROOT / "integrations" / "skills" / "paperseek" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Use the local PaperSeek Community product", skill)
        self.assertNotIn("scripts/", skill)

    def test_delivery_and_mcp_target_the_backend_distribution(self) -> None:
        backend = tomllib.loads(BACKEND_PROJECT.read_text(encoding="utf-8"))
        self.assertEqual(
            backend["tool"]["setuptools"]["packages"]["find"]["include"],
            ["paperseek_core*", "paperseek_app*", "paperseek_community*"],
        )
        self.assertEqual(
            backend["project"]["scripts"],
            {"paperseek": "paperseek_community.cli:main"},
        )

        mcp = tomllib.loads(
            (COMMUNITY_ROOT / "integrations" / "mcp" / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(mcp["project"]["version"], backend["project"]["version"])
        self.assertIn(
            f"paperseek=={backend['project']['version']}",
            mcp["project"]["dependencies"],
        )

        dockerfile = (COMMUNITY_ROOT / "Dockerfile").read_text(encoding="utf-8")
        publish = (
            COMMUNITY_ROOT / ".github" / "workflows" / "publish-pypi.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("COPY backend/pyproject.toml backend/uv.lock", dockerfile)
        self.assertIn("uv build --wheel --out-dir dist backend", publish)

    def test_release_metadata_and_legal_files_remain_complete(self) -> None:
        root_license = (COMMUNITY_ROOT / "LICENSE").read_text(encoding="utf-8")
        root_notices = (COMMUNITY_ROOT / "THIRD_PARTY_NOTICES.md").read_text(
            encoding="utf-8"
        )
        expected_urls = {
            "Homepage": "https://paperseek.app/",
            "Repository": "https://github.com/paperseek/paperseek",
            "Issues": "https://github.com/paperseek/paperseek/issues",
            "Documentation": "https://docs.paperseek.app/",
            "Online-Demo": "https://paperseek.app/",
        }
        for project_root in (
            COMMUNITY_ROOT / "backend",
            COMMUNITY_ROOT / "integrations" / "mcp",
        ):
            with self.subTest(project_root=project_root.relative_to(COMMUNITY_ROOT)):
                project = tomllib.loads(
                    (project_root / "pyproject.toml").read_text(encoding="utf-8")
                )["project"]
                self.assertEqual(project["license"], "Apache-2.0")
                self.assertEqual(
                    project["license-files"],
                    ["LICENSE", "THIRD_PARTY_NOTICES.md"],
                )
                self.assertEqual(project["urls"], expected_urls)
                self.assertTrue(project["authors"])
                self.assertTrue(project["maintainers"])
                self.assertTrue(project["keywords"])
                self.assertTrue(project["classifiers"])
                self.assertEqual(
                    (project_root / "LICENSE").read_text(encoding="utf-8"),
                    root_license,
                )
                self.assertEqual(
                    (project_root / "THIRD_PARTY_NOTICES.md").read_text(
                        encoding="utf-8"
                    ),
                    root_notices,
                )

    def test_delivery_metadata_and_documented_install_paths_remain_complete(self) -> None:
        dockerfile = (COMMUNITY_ROOT / "Dockerfile").read_text(encoding="utf-8")
        for expected in (
            "FROM node:22.14.0-alpine AS web-build",
            "FROM python:3.12.13-slim AS wheel-build",
            "FROM python:3.12.13-slim AS runtime",
            "COPY --from=web-build /build/web/dist ./src/paperseek_community/static",
            "uv build --wheel --out-dir /wheels",
            "python -m pip install --no-deps /wheels/paperseek-*.whl",
            "PAPERSEEK_DATABASE=/var/lib/paperseek/paperseek.db",
            'ENTRYPOINT ["paperseek"]',
            "EXPOSE 8765",
            'VOLUME ["/var/lib/paperseek"]',
            "/health",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, dockerfile)

        compose = (COMMUNITY_ROOT / "compose.yaml").read_text(encoding="utf-8")
        for expected in (
            "ghcr.io/paperseek/paperseek:${PAPERSEEK_VERSION:-1.0.0-beta3}",
            '"${PAPERSEEK_HOST:-127.0.0.1}:${PAPERSEEK_PORT:-8765}:8765"',
            "paperseek-data:/var/lib/paperseek",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, compose)
        self.assertNotIn("paperseek-mcp", compose)

        workflow = (
            COMMUNITY_ROOT / ".github" / "workflows" / "publish-ghcr.yml"
        ).read_text(encoding="utf-8")
        for expected in (
            "platform linux/amd64",
            "COMMUNITY_IMAGE: ghcr.io/paperseek/paperseek",
            "MCP_IMAGE: ghcr.io/paperseek/paperseek-mcp",
            "paperseek-data",
            "m4_official_client_integration.py",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, workflow)
        self.assertIn(
            '"${COMMUNITY_IMAGE}:${PAPERSEEK_VERSION}" \\\n'
            "            serve --database /var/lib/paperseek/paperseek.db "
            "--host 0.0.0.0 --port 8765 --no-worker",
            workflow,
        )

        public_docs = [
            COMMUNITY_ROOT / "README.md",
            COMMUNITY_ROOT / "CONTRIBUTING.md",
            COMMUNITY_ROOT / "integrations" / "skills" / "README.md",
            COMMUNITY_ROOT / "integrations" / "skills" / "paperseek" / "references" / "management-layer.md",
        ]
        for path in public_docs:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(COMMUNITY_ROOT)):
                self.assertNotRegex(text, re.compile(r"pip install -e \.($|\s)", re.MULTILINE))
                self.assertNotIn("python -m paperseek.cli", text)
        self.assertIn(
            "python -m pip install -e ./backend",
            (COMMUNITY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
