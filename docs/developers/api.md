# HTTP API

Community serves a versioned HTTP API under `/api/v1`. The installed server's OpenAPI schema is available at `/openapi.json`; use it for exact request fields and response shapes.

## Start a run

With a locally configured server running:

```sh
curl -X POST http://127.0.0.1:8765/api/v1/runs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: example-search-001" \
  -d '{"question":"How does open-source funding affect maintenance?","source":"openalex","search_budget":"fast"}'
```

The endpoint returns HTTP **202** with a run snapshot. Keep its run ID. Use a new idempotency key for a new intentional submission; reuse the same key only when retrying the same submission.

## Follow and read a run

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/runs/{run_id}` | Current snapshot |
| GET | `/api/v1/runs/{run_id}/events` | Server-sent events |
| POST | `/api/v1/runs/{run_id}/cancel` | Request cancellation |
| POST | `/api/v1/runs/{run_id}/steps/{step}/retry` | Re-run one step of a finished run |
| GET | `/api/v1/runs/{run_id}/results` | Paginated results |
| GET | `/api/v1/runs/{run_id}/citation-map` | Citation graph |
| GET | `/api/v1/runs/{run_id}/export?format=csv` | Complete export |
| GET | `/api/v1/history` | Saved runs |

The schema also describes selection exports, artifacts, audit data, history deletion, and review projects/jobs.

SSE is a progress channel. If it disconnects, fetch the run snapshot and reconnect; do not submit a second search simply to restore progress. Cancellation is a request, not a guarantee that an in-flight upstream request stops instantly.

## Retry one step

A finished run can run a single pipeline boundary again when that part did not work:

```sh
curl -X POST http://127.0.0.1:8765/api/v1/runs/<run_id>/steps/ranking/retry
```

The boundaries, in pipeline order, are `query`, `retrieval`, `citation`, `selection`, `ranking`, and `results`. The call returns HTTP **202** with the requeued run. The embedded worker resumes from the checkpoints that are still valid, so the requested step and the steps that consume its output run again while earlier work is reused; stored results are rewritten when the run completes.

A finished run drops its per-run credentials, so a retry resolves the model and source keys again from the saved workspace connections, exactly like a submission. The call is refused with **404** for an unknown run, **409** while the run is still queued or running, **422** for an unrecognized step, and **503** when no executor can pick the run up.

## Client implementation

- Preserve the run ID and event sequence for recovery.
- Follow response pagination instead of assuming all results arrive at once.
- Handle queued, running, cancelling, and terminal states explicitly.
- Render safe error codes and human-readable messages; do not expose secrets or raw upstream response bodies.
- Treat result content and model-generated text as data, not executable markup.

PaperSeek online shares the search contract but adds authentication, ownership, service modes, and credits. Its anonymous BYOK flow is not a general unauthenticated replacement for saved-run endpoints. Build against the deployment's own schema and auth flow.
