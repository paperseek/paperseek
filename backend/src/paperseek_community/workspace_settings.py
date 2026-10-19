"""SQLite storage for encrypted Community connections and workspace defaults."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from paperseek_app.settings import (
    SOURCE_OPTIONS,
    ConnectionInput,
    DefaultsInput,
    ProxySettingsInput,
    SettingsError,
    SourceSettingsInput,
    key_hint,
    normalize_route,
    requires_key,
)
from paperseek_app.source_catalog import SOURCE_BY_ID, SOURCES
from paperseek_app.submission import RunSubmission
from paperseek_community.credentials import EncryptedRunCredentialStore
from paperseek_community.settings import CommunityDeploymentSettings
from paperseek_core.sources.proxy import PROXY_SCOPES, ProxyPolicy, parse_proxy_scope, parse_proxy_urls

KEY_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkspaceSettingsStore:
    def __init__(self, database_path: Path | str, credentials: EncryptedRunCredentialStore) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.credentials = credentials

    # -- storage helpers -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _encrypt(self, scope: str, value: str) -> tuple[bytes | None, bytes | None]:
        if not value:
            return None, None
        nonce = os.urandom(12)
        aad = f"paperseek.workspace_settings|{scope}|{KEY_VERSION}".encode()
        return AESGCM(self.credentials.master_key()).encrypt(nonce, value.encode("utf-8"), aad), nonce

    def _decrypt(self, scope: str, ciphertext: bytes | None, nonce: bytes | None) -> str:
        if not ciphertext or not nonce:
            return ""
        aad = f"paperseek.workspace_settings|{scope}|{KEY_VERSION}".encode()
        return AESGCM(self.credentials.master_key()).decrypt(nonce, ciphertext, aad).decode("utf-8")

    @staticmethod
    def _test(row: sqlite3.Row) -> dict[str, object]:
        return {
            "status": row["test_status"],
            "latency_ms": row["test_latency_ms"],
            "code": row["test_code"],
            "tested_at": row["tested_at"],
        }

    # -- model connections -----------------------------------------------
    def _connection_rows(self) -> list[sqlite3.Row]:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT * FROM model_connections ORDER BY created_at, rowid").fetchall()

    def _public_connection(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "provider": row["provider"],
            "api_type": row["api_type"],
            "base_url": row["base_url"],
            "model": row["model"],
            "has_key": bool(row["ciphertext"]),
            "key_hint": key_hint(self._decrypt(f"connection|{row['id']}", row["ciphertext"], row["nonce"])),
            "requires_key": requires_key(row["kind"], row["provider"]),
            "test": self._test(row),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row(self, connection_id: str) -> sqlite3.Row:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM model_connections WHERE id = ?", (connection_id,)).fetchone()
        if row is None:
            raise SettingsError("connection_not_found", 404)
        return row

    def connection(self, connection_id: str) -> dict[str, object]:
        return self._public_connection(self._row(connection_id))

    def connection_secret(self, connection_id: str, kind: str | None = None) -> tuple[dict[str, str], str]:
        row = self._row(connection_id)
        if kind is not None and row["kind"] != kind:
            raise SettingsError("connection_kind_mismatch")
        route = {key: row[key] for key in ("provider", "api_type", "base_url", "model")}
        return route, self._decrypt(f"connection|{row['id']}", row["ciphertext"], row["nonce"])

    def create_connection(self, request: ConnectionInput) -> dict[str, object]:
        route = normalize_route(request.kind, request.provider, request.api_type, request.base_url, request.model)
        key = (request.api_key or "").strip()
        if requires_key(request.kind, route["provider"]) and not key:
            raise SettingsError("connection_key_required")
        connection_id = str(uuid4())
        ciphertext, nonce = self._encrypt(f"connection|{connection_id}", key)
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO model_connections (id, kind, provider, api_type, base_url, model,
                   ciphertext, nonce, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (connection_id, request.kind, route["provider"], route["api_type"], route["base_url"],
                 route["model"], ciphertext, nonce, now, now),
            )
            connection.commit()
        return self._public_connection(self._row(connection_id))

    def update_connection(self, connection_id: str, request: ConnectionInput) -> dict[str, object]:
        row = self._row(connection_id)
        if request.kind != row["kind"]:
            raise SettingsError("connection_kind_mismatch")
        route = normalize_route(request.kind, request.provider, request.api_type, request.base_url, request.model)
        same_endpoint = route["provider"] == row["provider"] and route["base_url"] == row["base_url"]
        if request.api_key is None and not same_endpoint and requires_key(request.kind, route["provider"]):
            # A stored key never follows a connection to a different endpoint.
            raise SettingsError("connection_key_required")
        key = (request.api_key if request.api_key is not None else
               self._decrypt(f"connection|{connection_id}", row["ciphertext"], row["nonce"])).strip()
        if requires_key(request.kind, route["provider"]) and not key:
            raise SettingsError("connection_key_required")
        ciphertext, nonce = self._encrypt(f"connection|{connection_id}", key)
        changed = key != self._decrypt(f"connection|{connection_id}", row["ciphertext"], row["nonce"]) or any(
            route[name] != row[name] for name in route
        )
        with closing(self._connect()) as connection:
            connection.execute(
                f"""UPDATE model_connections SET provider = ?, api_type = ?, base_url = ?, model = ?,
                   ciphertext = ?, nonce = ?, updated_at = ?
                   {", test_status = 'untested', test_latency_ms = NULL, test_code = '', tested_at = NULL" if changed else ""}
                   WHERE id = ?""",
                (route["provider"], route["api_type"], route["base_url"], route["model"],
                 ciphertext, nonce, _now(), connection_id),
            )
            connection.commit()
        return self._public_connection(self._row(connection_id))

    def delete_connection(self, connection_id: str, replacement: str = "") -> None:
        row = self._row(connection_id)
        defaults = self._stored_defaults()
        roles = [name for name in ("search_llm", "review_llm", "embedding", "reranker", "decision")
                 if self.defaults()[name] == connection_id]
        if roles:
            others = [item for item in self._connection_rows() if item["kind"] == row["kind"] and item["id"] != connection_id]
            if replacement:
                if not any(item["id"] == replacement for item in others):
                    raise SettingsError("connection_replacement_invalid")
            elif others:
                raise SettingsError("connection_in_use", 409)
            for name in roles:
                defaults[name] = replacement
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM model_connections WHERE id = ?", (connection_id,))
            self._write_defaults(connection, defaults)
            connection.commit()

    def clear_secrets(self) -> dict[str, int]:
        """Delete every saved model connection and data-source key; source options stay."""
        defaults = self._stored_defaults()
        for name in ("search_llm", "review_llm", "embedding", "reranker", "decision"):
            defaults.pop(name, None)
        # Without connections, only the built-in pre-ranking remains usable.
        defaults["pre_ranking"] = "local_fusion"
        proxies = len(self._saved_proxy()[0])
        with closing(self._connect()) as connection:
            connections = connection.execute("SELECT COUNT(*) FROM model_connections").fetchone()[0]
            sources = connection.execute("SELECT COUNT(*) FROM source_settings WHERE ciphertext IS NOT NULL").fetchone()[0]
            connection.execute("DELETE FROM model_connections")
            connection.execute("""UPDATE source_settings SET ciphertext = NULL, nonce = NULL, test_status = 'untested',
                test_latency_ms = NULL, test_code = '', tested_at = NULL WHERE ciphertext IS NOT NULL""")
            self._write_defaults(connection, defaults)
            # Proxy addresses can carry passwords, so they are cleared with the keys.
            connection.execute("DELETE FROM workspace_settings WHERE key = 'proxy'")
            connection.commit()
        return {"connections": connections, "source_keys": sources, "proxies": proxies}

    def record_connection_test(self, connection_id: str, result: dict[str, object]) -> None:
        self._record_test("model_connections", "id", connection_id, result)

    def _record_test(self, table: str, column: str, identity: str, result: dict[str, object]) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                f"""UPDATE {table} SET test_status = ?, test_latency_ms = ?, test_code = ?, tested_at = ?
                   WHERE {column} = ?""",
                ("ok" if result["ok"] else "failed", result.get("latency_ms"), result.get("code") or "",
                 _now(), identity),
            )
            connection.commit()

    # -- defaults ----------------------------------------------------------
    def _stored_defaults(self) -> dict[str, str]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value_json FROM workspace_settings WHERE key = 'defaults'").fetchone()
        value = json.loads(row["value_json"]) if row else {}
        return {str(key): str(item) for key, item in value.items() if isinstance(item, str)}

    @staticmethod
    def _write_defaults(connection: sqlite3.Connection, defaults: dict[str, str]) -> None:
        connection.execute(
            """INSERT INTO workspace_settings (key, value_json) VALUES ('defaults', ?)
               ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json""",
            (json.dumps(defaults, sort_keys=True),),
        )

    def defaults(self) -> dict[str, str]:
        """Stored choices, falling back to the first saved connection of each kind."""
        stored = self._stored_defaults()
        rows = self._connection_rows()
        ids = {kind: [row["id"] for row in rows if row["kind"] == kind] for kind in ("llm", "embedding", "reranker", "decision")}

        def pick(name: str, kind: str) -> str:
            value = stored.get(name, "")
            return value if value in ids[kind] else (ids[kind][0] if ids[kind] else "")

        usable = [source["id"] for source in self.sources() if source["usable"]]
        source = stored.get("source", "")
        if source not in usable:
            source = "openalex" if "openalex" in usable else (usable[0] if usable else "")
        mode = stored.get("pre_ranking", "local_fusion")
        return {
            "search_llm": pick("search_llm", "llm"),
            "review_llm": pick("review_llm", "llm"),
            "pre_ranking": mode if mode in {"local_fusion", "embedding_rerank", "decision"} else "local_fusion",
            "embedding": pick("embedding", "embedding"),
            "reranker": pick("reranker", "reranker"),
            "decision": pick("decision", "decision"),
            "source": source,
        }

    def save_defaults(self, request: DefaultsInput) -> dict[str, str]:
        stored = self._stored_defaults()
        rows = {row["id"]: row["kind"] for row in self._connection_rows()}
        kinds = {"search_llm": "llm", "review_llm": "llm", "embedding": "embedding",
                 "reranker": "reranker", "decision": "decision"}
        for name, value in request.model_dump(exclude_none=True).items():
            if name in kinds and value and rows.get(value) != kinds[name]:
                raise SettingsError("connection_not_found", 404)
            if name == "source" and value and not any(item["id"] == value and item["usable"] for item in self.sources()):
                raise SettingsError("source_unavailable")
            stored[name] = value
        with closing(self._connect()) as connection:
            self._write_defaults(connection, stored)
            connection.commit()
        return self.defaults()

    # -- data sources ------------------------------------------------------
    def _source_rows(self) -> dict[str, sqlite3.Row]:
        with closing(self._connect()) as connection:
            return {row["source_id"]: row for row in connection.execute("SELECT * FROM source_settings")}

    def sources(self, deployment: CommunityDeploymentSettings | None = None) -> list[dict[str, object]]:
        from paperseek_community.composition import deployment_source_key

        rows = self._source_rows()
        result = []
        for definition in SOURCES:
            row = rows.get(definition.id)
            stored_key = bool(row and row["ciphertext"])
            environment_key = bool(deployment and deployment_source_key(definition.id, deployment))
            has_key = stored_key or environment_key
            options = json.loads(row["options_json"]) if row else {}
            result.append({
                "id": definition.id,
                "has_key": has_key,
                "key_hint": key_hint(self._decrypt(f"source|{definition.id}", row["ciphertext"], row["nonce"])) if stored_key else "",
                "key_origin": "settings" if stored_key else "environment" if environment_key else "",
                "usable": definition.api_key != "required" or has_key,
                "options": {name: str(options.get(name, "")) for name in SOURCE_OPTIONS.get(definition.id, ())},
                "test": self._test(row) if row else {"status": "untested", "latency_ms": None, "code": "", "tested_at": None},
            })
        return result

    def source_secret(self, source_id: str) -> tuple[str, dict[str, str]]:
        row = self._source_rows().get(source_id)
        if row is None:
            return "", {}
        options = json.loads(row["options_json"])
        return self._decrypt(f"source|{source_id}", row["ciphertext"], row["nonce"]), {
            str(key): str(value) for key, value in options.items()
        }

    def save_source(self, source_id: str, request: SourceSettingsInput) -> None:
        definition = SOURCE_BY_ID.get(source_id)
        if definition is None:
            raise SettingsError("source_not_found", 404)
        current_key, current_options = self.source_secret(source_id)
        key = current_key if request.api_key is None else request.api_key.strip()
        if key and definition.api_key == "not_required":
            raise SettingsError("source_key_not_supported")
        options = dict(current_options)
        if request.options is not None:
            allowed = SOURCE_OPTIONS.get(source_id, ())
            if any(name not in allowed for name in request.options):
                raise SettingsError("source_option_unsupported")
            options.update({name: value.strip()[:320] for name, value in request.options.items()})
        ciphertext, nonce = self._encrypt(f"source|{source_id}", key)
        changed = key != current_key or options != current_options
        with closing(self._connect()) as connection:
            connection.execute(
                f"""INSERT INTO source_settings (source_id, ciphertext, nonce, options_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET ciphertext = excluded.ciphertext,
                   nonce = excluded.nonce, options_json = excluded.options_json, updated_at = excluded.updated_at
                   {", test_status = 'untested', test_latency_ms = NULL, test_code = '', tested_at = NULL" if changed else ""}""",
                (source_id, ciphertext, nonce, json.dumps(options, sort_keys=True), _now()),
            )
            connection.commit()

    def record_source_test(self, source_id: str, result: dict[str, object]) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO source_settings (source_id, updated_at) VALUES (?, ?)", (source_id, _now()),
            )
            connection.commit()
        self._record_test("source_settings", "source_id", source_id, result)

    # -- network proxy -----------------------------------------------------
    def _saved_proxy(self) -> tuple[tuple[str, ...], frozenset[str]]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value_json FROM workspace_settings WHERE key = 'proxy'").fetchone()
        if not row:
            return (), frozenset(PROXY_SCOPES)
        value = json.loads(row["value_json"])
        urls = self._decrypt("proxy", base64.b64decode(value["ciphertext"]), base64.b64decode(value["nonce"]))
        return parse_proxy_urls(urls), parse_proxy_scope(value.get("scope") or [], default=())

    def proxy_policy(self, deployment: CommunityDeploymentSettings | None = None) -> ProxyPolicy:
        """The deployment environment wins over addresses saved in settings."""
        if deployment is not None and deployment.proxy.urls:
            return deployment.proxy
        urls, scope = self._saved_proxy()
        return ProxyPolicy(urls, scope)

    def proxy(self, deployment: CommunityDeploymentSettings | None = None) -> dict[str, object]:
        environment = deployment is not None and bool(deployment.proxy.urls)
        policy = self.proxy_policy(deployment)
        hosts = []
        for url in policy.urls:
            parts = urlsplit(url)
            hosts.append(f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname))
        return {
            "origin": "environment" if environment else "settings" if policy.urls else "",
            "hosts": hosts,
            "scope": [scope for scope in PROXY_SCOPES if scope in policy.scope],
        }

    def save_proxy(self, request: ProxySettingsInput, deployment: CommunityDeploymentSettings | None = None) -> dict[str, object]:
        if deployment is not None and deployment.proxy.urls:
            raise SettingsError("proxy_set_by_environment", 409)
        current, _ = self._saved_proxy()
        try:
            urls = current if request.urls is None else parse_proxy_urls(request.urls)
        except ValueError:
            raise SettingsError("proxy_url_invalid") from None
        with closing(self._connect()) as connection:
            if urls:
                ciphertext, nonce = self._encrypt("proxy", "\n".join(urls))
                value = {"ciphertext": base64.b64encode(ciphertext).decode(), "nonce": base64.b64encode(nonce).decode(),
                         "scope": [scope for scope in PROXY_SCOPES if scope in request.scope]}
                connection.execute(
                    """INSERT INTO workspace_settings (key, value_json) VALUES ('proxy', ?)
                       ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json""",
                    (json.dumps(value, sort_keys=True),),
                )
            else:
                connection.execute("DELETE FROM workspace_settings WHERE key = 'proxy'")
            connection.commit()
        return self.proxy(deployment)

    # -- composite views ---------------------------------------------------
    def snapshot(self, deployment: CommunityDeploymentSettings) -> dict[str, object]:
        return {
            "connections": [self._public_connection(row) for row in self._connection_rows()],
            "sources": self.sources(deployment),
            "defaults": self.defaults(),
            "proxy": self.proxy(deployment),
        }

    def apply_to_submission(self, submission: RunSubmission) -> RunSubmission:
        """Replace saved-connection ids with routes and per-run keys."""
        data = submission.model_dump(mode="python")
        selection = data.pop("connections", None) or {}
        credentials = data["credentials"]
        if selection.get("llm"):
            route, key = self.connection_secret(selection["llm"], "llm")
            data["llm"] = route
            credentials["llm"]["api_key"] = key
        retrieval = data["retrieval"]
        mode = retrieval.get("mode")
        kinds = ("embedding", "reranker") if mode == "embedding_rerank" else ("decision",) if mode == "decision" else ()
        for kind in kinds:
            if selection.get(kind):
                route, key = self.connection_secret(selection[kind], kind)
                retrieval[kind] = {"provider": route["provider"], "model": route["model"], "base_url": route["base_url"]}
                credentials[kind]["api_key"] = key
        key, options = self.source_secret(data["source"])
        if not credentials["source"]["api_key"]:
            credentials["source"]["api_key"] = key
        contacts = data["source_contacts"]
        for name in ("openalex_email", "crossref_email", "pubmed_email"):
            if not contacts.get(name) and options.get(name):
                contacts[name] = options[name]
        if options.get("pubmed_tool") and contacts.get("pubmed_tool") in {"", "paperseek"}:
            contacts["pubmed_tool"] = options["pubmed_tool"]
        if not data.get("wos_db") and options.get("wos_db"):
            data["wos_db"] = options["wos_db"]
        return RunSubmission.model_validate(data)
