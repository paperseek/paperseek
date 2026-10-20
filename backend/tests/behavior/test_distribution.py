"""Distribution metadata that users and package indexes consume."""

import tomllib
import unittest
from pathlib import Path

COMMUNITY_ROOT = Path(__file__).resolve().parents[3]


class DistributionContract(unittest.TestCase):
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



if __name__ == "__main__":
    unittest.main()
