"""Reproduce downloadable formats from a verified, complete canonical result."""
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from paperseek_app.errors import ArtifactIntegrityError, ArtifactNotFound
from paperseek_app.models import ArtifactContent, ArtifactDescriptor, StoredArtifact
from paperseek_core.final_result import FinalResultAssembler
from paperseek_core.models import ResearchResult


EXPORT_KINDS = frozenset({"export_json", "export_csv", "export_bibtex", "export_ris", "candidate_pool", "citation_map"})


def export_id(primary: StoredArtifact, kind: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"paperseek-derived:{primary.id}:{kind}"))


def derive_exports(primary: StoredArtifact, content: bytes):
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("A final result must be an object.")
        # Older public results omitted export inputs. Keep their existing stored files.
        if not all(field in payload for field in ("query_iteration_memory", "warnings", "provenance_by_identity")):
            raise ArtifactNotFound("This historical result requires its stored export.")
        result = ResearchResult.model_validate(payload)
    except ValueError:
        raise ArtifactIntegrityError("The canonical final result is invalid.") from None
    exports = {}
    for draft in FinalResultAssembler().artifacts_for(result):
        if draft.kind not in EXPORT_KINDS:
            continue
        body = draft.content.encode("utf-8")
        identity = export_id(primary, draft.kind)
        digest = hashlib.sha256(body).hexdigest()
        descriptor = ArtifactDescriptor(id=identity, kind=draft.kind, filename=draft.filename,
            media_type=draft.media_type, sha256=digest, size=len(body),
            schema_version=primary.schema_version, created_at=primary.created_at,
            download_url=f"/api/v1/runs/{primary.run_id}/artifacts/{identity}")
        content = ArtifactContent(id=identity, filename=draft.filename, media_type=draft.media_type,
                                 sha256=digest, size=len(body), content=body)
        exports[draft.kind] = (descriptor, content)
    return exports
