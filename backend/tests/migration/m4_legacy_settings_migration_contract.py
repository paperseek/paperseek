from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paperseek_community.cli import main as cli_main
from paperseek_community.diagnostics import community_doctor_report
from paperseek_community.legacy_settings import (
    LEGACY_SETTING_ALLOWLIST,
    LegacySettingsMigrationError,
    migrate_legacy_env,
)
from paperseek_community.settings import CommunityDeploymentSettings


EXPECTED_ALLOWLIST = {
    "LLM_API_KEY": "PAPERSEEK_LLM_API_KEY",
    "LLM_API_TYPE": "PAPERSEEK_LLM_API_TYPE",
    "LLM_BASE_URL": "PAPERSEEK_LLM_BASE_URL",
    "LLM_MODEL": "PAPERSEEK_LLM_MODEL",
    "OPENALEX_API_KEY": "PAPERSEEK_OPENALEX_API_KEY",
    "PUBMED_API_KEY": "PAPERSEEK_PUBMED_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY": "PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY",
    "SERPER_API_KEY": "PAPERSEEK_GOOGLE_SCHOLAR_API_KEY",
    "SERPER_API_KEYS": "PAPERSEEK_GOOGLE_SCHOLAR_API_KEY",
    "WOS_API_KEY": "PAPERSEEK_WOS_API_KEY",
    "WOS_DB": "PAPERSEEK_WOS_DB",
}


def _parse_env(path: Path) -> dict[str, str]:
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


class M4LegacySettingsMigrationContract(unittest.TestCase):
    def test_allowlist_is_explicit_and_runtime_does_not_dual_read_old_names(self) -> None:
        self.assertEqual(LEGACY_SETTING_ALLOWLIST, EXPECTED_ALLOWLIST)
        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "legacy-secret",
                "OPENALEX_API_KEY": "legacy-openalex-secret",
                "WOS_DB": "WOK",
            },
            clear=True,
        ):
            settings = CommunityDeploymentSettings.from_environment()

        self.assertEqual(settings.llm_api_key, "")
        self.assertEqual(settings.openalex_api_key, "")
        self.assertEqual(settings.wos_db, "WOS")

    def test_migration_maps_same_semantics_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "legacy.env"
            target = root / "paperseek-v0.3.env"
            source.write_text(
                "\n".join(
                    [
                        "LLM_API_TYPE=openai_chat",
                        "LLM_BASE_URL=https://llm.example.invalid/v1",
                        "LLM_MODEL=controlled-model",
                        "LLM_API_KEY=legacy-llm-secret",
                        "OPENALEX_API_KEY=legacy-openalex-secret",
                        "SEMANTIC_SCHOLAR_API_KEY=legacy-s2-secret",
                        "PUBMED_API_KEY=legacy-pubmed-secret",
                        "WOS_API_KEY=legacy-wos-secret",
                        "WOS_DB=WOK",
                        "SERPER_API_KEY=shadowed-single-secret",
                        "SERPER_API_KEYS=serper-a,serper-b",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = migrate_legacy_env(source, target)
            values = _parse_env(target)

        self.assertEqual(report["status"], "migrated")
        self.assertEqual(report["mapped_settings"], sorted(EXPECTED_ALLOWLIST))
        self.assertEqual(report["shadowed_settings"], ["SERPER_API_KEY"])
        self.assertEqual(values["PAPERSEEK_LLM_API_KEY"], "legacy-llm-secret")
        self.assertEqual(values["PAPERSEEK_OPENALEX_API_KEY"], "legacy-openalex-secret")
        self.assertEqual(values["PAPERSEEK_WOS_DB"], "WOK")
        self.assertEqual(values["PAPERSEEK_GOOGLE_SCHOLAR_API_KEY"], "serper-a,serper-b")
        serialized = json.dumps(report, sort_keys=True)
        for secret in (
            "legacy-llm-secret",
            "legacy-openalex-secret",
            "serper-a",
            "shadowed-single-secret",
        ):
            self.assertNotIn(secret, serialized)

    def test_replay_is_idempotent_and_conflict_does_not_overwrite_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "legacy.env"
            target = root / "paperseek-v0.3.env"
            source.write_text("LLM_API_KEY=legacy-secret\n", encoding="utf-8")

            first = migrate_legacy_env(source, target)
            before = target.read_bytes()
            replay = migrate_legacy_env(source, target)
            self.assertEqual(target.read_bytes(), before)

            source.write_text("LLM_API_KEY=different-secret\n", encoding="utf-8")
            with self.assertRaises(LegacySettingsMigrationError) as raised:
                migrate_legacy_env(source, target)

            self.assertEqual(first["status"], "migrated")
            self.assertEqual(replay["status"], "already_migrated")
            self.assertEqual(raised.exception.code, "target_setting_conflict")
            self.assertEqual(raised.exception.settings, ("PAPERSEEK_LLM_API_KEY",))
            self.assertNotIn("different-secret", str(raised.exception))
            self.assertEqual(target.read_bytes(), before)

    def test_unknown_source_settings_are_ignored_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "legacy.env"
            target = root / "paperseek-v0.3.env"
            source.write_text(
                "DATA_SOURCE=wos\n"
                "RETRIEVAL_POOL_MAX=private-pool-value\n"
                "UNMAPPED_LEGACY_SETTING=private-retry-value\n"
                "PAPERSEEK_CONTACT_EMAIL=maintainer@example.invalid\n",
                encoding="utf-8",
            )

            report = migrate_legacy_env(source, target)
            values = _parse_env(target)

            self.assertEqual(
                report["ignored_settings"],
                ["DATA_SOURCE", "RETRIEVAL_POOL_MAX", "UNMAPPED_LEGACY_SETTING"],
            )
            self.assertEqual(
                values,
                {"PAPERSEEK_CONTACT_EMAIL": "maintainer@example.invalid"},
            )
            serialized = json.dumps(report, sort_keys=True)
            self.assertNotIn("private-pool-value", serialized)
            self.assertNotIn("private-retry-value", serialized)

    def test_target_may_hold_every_setting_the_runtime_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "legacy.env"
            target = root / "paperseek-v0.3.env"
            source.write_text("LLM_API_KEY=legacy-secret\n", encoding="utf-8")
            target.write_text(
                "PAPERSEEK_VERSION=1.0.0-beta5\nPAPERSEEK_WANFANG_API_KEY=wanfang\nPAPERSEEK_PROXY_URLS=http://proxy.example:80\n"
                "PAPERSEEK_PROXY_SCOPE=models\nPAPERSEEK_MAX_CONCURRENT_RUNS=2\n",
                encoding="utf-8",
            )

            migrate_legacy_env(source, target)

            written = target.read_text(encoding="utf-8")
            self.assertIn("PAPERSEEK_LLM_API_KEY", written)
            self.assertIn("PAPERSEEK_PROXY_URLS", written)
            # Docker Compose keeps its own variables in the same file.
            self.assertIn('PAPERSEEK_VERSION="1.0.0-beta5"', written)

    def test_unknown_target_setting_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "legacy.env"
            target = root / "paperseek-v0.3.env"
            source.write_text("LLM_API_KEY=legacy-secret\n", encoding="utf-8")
            target.write_text("UNKNOWN_TARGET_SETTING=preserve-me\n", encoding="utf-8")
            before = target.read_bytes()

            with self.assertRaises(LegacySettingsMigrationError) as raised:
                migrate_legacy_env(source, target)

            self.assertEqual(raised.exception.code, "unsupported_target_setting")
            self.assertEqual(raised.exception.settings, ("UNKNOWN_TARGET_SETTING",))
            self.assertEqual(target.read_bytes(), before)
            self.assertNotIn("legacy-secret", str(raised.exception))

    def test_doctor_reports_names_only_and_cli_is_a_one_time_migrate_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "legacy.env"
            target = root / "paperseek-v0.3.env"
            database = root / "paperseek.db"
            source.write_text("export LLM_API_KEY=legacy-secret\nWOS_DB=\"WOK\"\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "LLM_API_KEY": "legacy-secret",
                    "DATA_SOURCE": "wos",
                    "UNMAPPED_LEGACY_SETTING": "auto",
                },
                clear=True,
            ):
                doctor = community_doctor_report(
                    database,
                    CommunityDeploymentSettings.from_environment(),
                    source="openalex",
                )
            legacy_check = next(
                check for check in doctor["checks"] if check["id"] == "configuration.legacy_settings"
            )
            self.assertEqual(legacy_check["status"], "warning")
            self.assertEqual(
                legacy_check["details"],
                {
                    "error_code": "legacy_settings_detected",
                    "mappable_settings": ["LLM_API_KEY"],
                },
            )
            self.assertNotIn("legacy-secret", json.dumps(doctor, sort_keys=True))

            with patch.dict(
                os.environ,
                {
                    "DATA_SOURCE": "wos",
                    "PAPERSEEK_LLM_API_KEY": "current-secret",
                    "UNMAPPED_LEGACY_SETTING": "auto",
                },
                clear=True,
            ):
                ignored_only_doctor = community_doctor_report(
                    database,
                    CommunityDeploymentSettings.from_environment(),
                    source="openalex",
                )
            self.assertNotIn(
                "configuration.legacy_settings",
                {check["id"] for check in ignored_only_doctor["checks"]},
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "migrate",
                        "--legacy-env",
                        str(source),
                        "--output-env",
                        str(target),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "migrated")
            self.assertNotIn("legacy-secret", stdout.getvalue())
            self.assertEqual(_parse_env(target)["PAPERSEEK_WOS_DB"], "WOK")

            ignored = root / "ignored.env"
            ignored.write_text(
                "UNMAPPED_LEGACY_SETTING=private-retry-value\n"
                "DATA_SOURCE=wos\n",
                encoding="utf-8",
            )
            ignored_target = root / "ignored-output.env"
            ignored_stdout = io.StringIO()
            ignored_stderr = io.StringIO()
            with redirect_stdout(ignored_stdout), redirect_stderr(ignored_stderr):
                exit_code = cli_main(
                    [
                        "migrate",
                        "--legacy-env",
                        str(ignored),
                        "--output-env",
                        str(ignored_target),
                    ]
                )
            ignored_report = json.loads(ignored_stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                ignored_report["ignored_settings"],
                ["DATA_SOURCE", "UNMAPPED_LEGACY_SETTING"],
            )
            self.assertTrue(ignored_target.is_file())
            self.assertEqual(ignored_stderr.getvalue(), "")
            self.assertNotIn("private-retry-value", ignored_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
