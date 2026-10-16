import type { Page, Route } from "@playwright/test";

// One in-memory stand-in for the Community API. Specs adjust `state` directly.
const now = new Date().toISOString();
const finished = new Set(["completed", "completed_with_warnings", "failed", "cancelled"]);

export const ids = {
  done: "11111111-1111-4111-8111-111111111111",
  small: "22222222-2222-4222-8222-222222222222",
  running: "33333333-3333-4333-8333-333333333333",
  queued: "44444444-4444-4444-8444-444444444444",
  failed: "55555555-5555-4555-8555-555555555555",
  analysis: "66666666-6666-4666-8666-666666666666",
  review: "77777777-7777-4777-8777-777777777777",
  llm: "88888888-8888-4888-8888-888888888888",
  llm2: "99999999-9999-4999-8999-999999999999",
};

type Json = Record<string, any>;

function descriptor(id: string, label: string, api_key: string, extra: Json = {}): Json {
  return {
    id, label, api_key, status: id === "openalex" ? "default" : "supported", default: id === "openalex",
    credential: api_key === "not_required" ? null : { input_label: `${label} API Key`, guidance_label: "Get key", guidance_url: "https://example.test/key" },
    capabilities: { citation_expansion: id === "openalex", filter: { mode: id === "openalex" ? "native" : id === "pubmed" ? "text" : "none", label: "Field", options: id === "openalex" ? [
      { id: "17", label: "Computer Science", domain: "3" }, { id: "27", label: "Medicine", domain: "4" },
    ] : [] } },
    ...extra,
  };
}

export function paper(index: number, extra: Json = {}): Json {
  return {
    uid: `uid-${index}`, rank: index, title: `Paper ${index} on screening`, authors: [`Chen L${index}`, "Park S"],
    publish_year: 2020 + (index % 5), venue: "Research Synthesis Methods", abstract: `Abstract ${index} about screening workloads.`,
    score: 10 - (index % 6), reasoning: `Reason ${index}.`, citations: String(index * 3), doi: `10.1000/${index}`,
    url: `https://example.test/${index}`, provider: "openalex", ...extra,
  };
}

function runDetail(id: string, question: string, status: string, extra: Json = {}): Json {
  return {
    id, kind: "research", question, display_title: "", source: "openalex", status, current_step: "", attempt: 0,
    lease_generation: 0, lease_owner: "", lease_expires_at: null, last_event_sequence: 0, error_code: "", warnings: [],
    created_at: now, deadline_at: now, started_at: now, cancel_requested_at: null, finished_at: null, updated_at: now,
    result_count: 0, queue: null,
    config_snapshot: { question, source: "openalex", search_budget: "medium", citation_enabled: true,
      llm: { provider: "deepseek", api_type: "openai_chat", base_url: "https://api.deepseek.com", model: "deepseek-chat" },
      retrieval: { mode: "local_fusion" } },
    ...extra,
  };
}

export function event(runId: string, sequence: number, event_type: string, payload: Json = {}, stage = "") {
  return { run_id: runId, sequence, event_type, stage: stage || event_type.split(".")[0], occurred_at: new Date(Date.now() - 30_000).toISOString(), schema_version: 1, payload };
}

export function completedEvents(runId: string) {
  return [
    event(runId, 1, "query.started", { target_min: 5 }),
    event(runId, 2, "query.planned", { intent: "Compare LLM screening with humans", current_query: "llm AND screening", core_concepts: ["LLM", "screening"] }),
    event(runId, 3, "query.progress", { iteration: 1, decision: "accept", hit_count: 12, total: 31, query: "llm AND screening" }),
    event(runId, 4, "retrieval.completed", { candidate_count: 24, total_candidates: 31, completed_lanes: 2, total_lanes: 2 }),
    event(runId, 5, "ranking.started", { candidate_count: 24, batch_count: 2 }),
    event(runId, 6, "ranking.completed", { candidate_count: 24, scored_count: 24, warning_count: 0 }),
    event(runId, 7, "workflow.completed", { result_count: 6, warning_count: 0 }),
  ];
}

function connection(id: string, model: string, extra: Json = {}): Json {
  return {
    id, kind: "llm", provider: "deepseek", api_type: "openai_chat", base_url: "https://api.deepseek.com", model, has_key: true,
    requires_key: true, test: { status: "ok", latency_ms: 412, code: "", tested_at: now }, created_at: now, updated_at: now, ...extra,
  };
}

export function createState() {
  const sources = [
    descriptor("openalex", "OpenAlex", "recommended"),
    descriptor("arxiv", "arXiv", "not_required"),
    descriptor("pubmed", "PubMed", "optional"),
    descriptor("wos", "Web of Science Starter", "required", { status: "temporarily_unavailable" }),
  ];
  const results: Record<string, Json[]> = {
    [ids.done]: Array.from({ length: 6 }, (_, i) => paper(i + 1, i === 3 ? { abstract: "", pdf_url: "https://example.test/4.pdf" } : {})),
    [ids.small]: [paper(1)],
  };
  return {
    sources,
    connections: [connection(ids.llm, "deepseek-chat"), connection(ids.llm2, "qwen-plus", { provider: "dashscope", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", test: { status: "failed", latency_ms: 90, code: "http_401", tested_at: now } })] as Json[],
    sourceSettings: sources.map((item) => ({
      id: item.id, has_key: false, key_origin: "", usable: item.api_key !== "required",
      options: item.id === "openalex" ? { openalex_email: "" } : item.id === "pubmed" ? { pubmed_tool: "paperseek", pubmed_email: "" } : item.id === "wos" ? { wos_db: "" } : {},
      test: item.id === "openalex" ? { status: "ok", latency_ms: 290, code: "", tested_at: now } : { status: "untested", latency_ms: null, code: "", tested_at: null },
    })) as Json[],
    defaults: { search_llm: ids.llm, review_llm: ids.llm, pre_ranking: "local_fusion", embedding: "", reranker: "", decision: "", source: "openalex" } as Json,
    runs: {
      [ids.done]: runDetail(ids.done, "LLM screening for systematic reviews", "completed", { result_count: 6, last_event_sequence: 7 }),
      [ids.small]: runDetail(ids.small, "Tiny search", "completed", { result_count: 1 }),
      [ids.failed]: runDetail(ids.failed, "Failed search", "failed", { error_code: "provider_authentication_failed", last_event_sequence: 2 }),
    } as Record<string, Json>,
    events: {
      [ids.done]: completedEvents(ids.done),
      [ids.failed]: [
        event(ids.failed, 1, "query.started", {}),
        event(ids.failed, 2, "run.failed", { code: "provider_authentication_failed" }, "lifecycle"),
      ],
    } as Record<string, Json[]>,
    results,
    analyses: [] as Json[],
    reviews: [] as Json[],
    submissions: [] as Json[],
    retries: [] as Array<{ run_id: string; step: string }>,
    // When true the retry route leaves the run queued so a spec can observe it.
    retryManual: false,
    requests: [] as Array<{ method: string; path: string; body: unknown; query: URLSearchParams }>,
    probe: { ok: true, latency_ms: 386, code: "" } as Json,
    deleted: [] as string[],
    seedTitle: "Seed paper",
  };
}
export type MockState = ReturnType<typeof createState>;

function settingsPayload(state: MockState) {
  return { connections: state.connections, sources: state.sourceSettings, defaults: state.defaults };
}
function historyItems(state: MockState) {
  return Object.values(state.runs).map((run) => ({
    id: run.id, question: run.question, display_title: run.display_title, source: run.source, status: run.status,
    result_count: run.result_count, created_at: run.created_at, warnings: run.warnings, error_code: run.error_code,
  }));
}
function analysis(state: MockState, sourceRunId: string): Json {
  const bucket = (label: string, count: number) => ({ label, count });
  return {
    id: ids.analysis, title: state.runs[sourceRunId]?.question || "Analysis", source_run_id: sourceRunId, paper_count: 642, scope: "corpus",
    created_at: now, source_reported_total: 18906, warnings: [],
    statistics: {
      paper_count: 642, scored_count: 120, citation_sum: 18240,
      years: [bucket("2023", 40), bucket("2024", 80), bucket("2025", 60)],
      venues: Array.from({ length: 9 }, (_, i) => bucket(`Venue ${i + 1}`, 40 - i)),
      document_types: [bucket("article", 468)], providers: [bucket("openalex", 642)],
      authors: [bucket("Thomas J", 9)], institutions: [bucket("UCL", 14)], countries: [bucket("US", 164)],
      keywords: ["systematic review", "llm", "screening", "machine learning", "reproducibility", "recall", "bias"].map((label, i) => bucket(label, 148 - i * 10)),
      keyword_sources: [bucket("openalex:keywords", 600)], citation_sources: [bucket("openalex", 640)],
      coverage: [{ field: "citations", available: 640, total: 642 }, { field: "doi", available: 600, total: 642 }],
      category_totals: { venue: 214, keywords: 1204 },
    },
  };
}
function reviewDetail(state: MockState, body: Json): Json {
  const papers = (body.paper_uids as string[]).map((uid, index) => {
    const source = (state.results[body.source_run_id] || []).find((item) => item.uid === uid) || paper(index + 1);
    return { id: `p${index + 1}`, title: source.title, authors: source.authors, year: source.publish_year, has_abstract: Boolean(source.abstract),
      abstract: source.abstract, abstract_hash: "h", source_run_id: body.source_run_id, source_uid: uid, url: source.url, source_rank: index + 1 };
  });
  return {
    id: ids.review, version: 1, source_run_id: body.source_run_id, display_title: "", active_job: null, latest_job: null,
    content: { topic: body.topic, language: body.language, length: body.length, section_count: body.section_count, papers, corpus_hash: "c",
      cards: papers.map((item) => ({ paper_id: item.id, evidence: [{ id: `e-${item.id}`, paper_id: item.id, quote: `Quote from ${item.title}` }] })),
      outline: null, outline_revision: 0, sections: [], evidence_errors: {} },
    document: { sections: [], references: [], citation_style: "numeric", draft: false },
  };
}
export function withOutline(review: Json): Json {
  const ids = review.content.papers.map((item: Json) => item.id);
  review.content.outline = { focus: "Compare recall and workload.", sections: [
    { id: "s1", title: "Screening performance", kind: "theme", parent_id: "", goal: "Compare recall.", comparisons: ["Recall"], paper_ids: ids, paragraphs: [{ goal: "Summarize recall.", paper_ids: ids }] },
    { id: "s2", title: "Reproducibility", kind: "theme", parent_id: "", goal: "Discuss bias.", comparisons: [], paper_ids: ids, paragraphs: [{ goal: "Discuss bias.", paper_ids: ids }] },
  ] };
  review.content.outline_revision = 1;
  return review;
}
export function withDocument(review: Json): Json {
  withOutline(review);
  const citation = { number: 1, label: "[1]", paper_id: review.content.papers[0].id, evidence_ids: [`e-${review.content.papers[0].id}`] };
  review.content.sections = review.content.outline.sections.map((section: Json) => ({ section_id: section.id, revision: 1, body: { paragraphs: [{ text: "Text.", evidence_ids: citation.evidence_ids }] }, findings: [] }));
  review.content.consistency_signature = "ok";
  review.content.consistency_findings = [{ reason: "Section 2 repeats a claim from section 1." }];
  review.document = {
    draft: false, citation_style: "numeric",
    sections: review.content.outline.sections.map((section: Json) => ({ id: section.id, title: section.title, level: 1, validation: "supported", stale: false,
      paragraphs: [{ spans: [{ text: "LLMs reach high recall " }, { citation }, { text: "." }], citation_warnings: [] }] })),
    references: [{ number: 1, text: "Chen L. Paper 1. 2021.", url: "https://example.test/1", doi: "10.1000/1" }],
  };
  return review;
}

async function respond(route: Route, body: unknown, status = 200) {
  if (status === 204) return route.fulfill({ status: 204, body: "" });
  return route.fulfill({ status, json: body });
}
function error(code: string, status = 422) {
  return { error: { code, message: code, request_id: "test", details: {} } };
}

export async function mockBackend(page: Page, state: MockState = createState()): Promise<MockState> {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = request.method();
    const body = request.postData() ? (() => { try { return JSON.parse(request.postData()!); } catch { return request.postData(); } })() : null;
    state.requests.push({ method, path, body, query: url.searchParams });
    const parts = path.split("/").filter(Boolean);

    if (path === "/sources") return respond(route, { sources: state.sources });
    if (path === "/data/summary") {
      const runs = Object.values(state.runs);
      return respond(route, { searches: runs.filter((run) => finished.has(String(run.status))).length, analyses: state.analyses.length,
        reviews: state.reviews.length, active: runs.filter((run) => !finished.has(String(run.status))).length, location: "~/.paperseek" });
    }
    if (path === "/data/history/clear") {
      const active = Object.values(state.runs).filter((run) => !finished.has(String(run.status)));
      if (active.length) {
        // Cancellation is requested now and observed as finished by the next call.
        for (const run of active) run.status = "cancelled";
        return respond(route, { status: "stopping", active: active.length, searches: 0, analyses: 0, reviews: 0 });
      }
      const counts = { searches: Object.keys(state.runs).length, analyses: state.analyses.length, reviews: state.reviews.length };
      state.runs = {};
      state.analyses = [];
      state.reviews = [];
      return respond(route, { status: "cleared", active: 0, ...counts });
    }
    if (path === "/settings/secrets" && method === "DELETE") {
      const cleared = { connections: state.connections.length, source_keys: state.sourceSettings.filter((item) => item.key_origin === "settings").length };
      state.connections = [];
      for (const item of state.sourceSettings) if (item.key_origin === "settings") Object.assign(item, { has_key: false, key_origin: "" });
      Object.assign(state.defaults, { search_llm: "", review_llm: "", embedding: "", reranker: "", decision: "", pre_ranking: "local_fusion" });
      return respond(route, cleared);
    }
    if (path === "/settings" && method === "GET") return respond(route, settingsPayload(state));
    if (path === "/settings/defaults") {
      Object.assign(state.defaults, Object.fromEntries(Object.entries(body as Json).filter(([, value]) => value !== null)));
      return respond(route, state.defaults);
    }
    if (path === "/settings/connections/test") return respond(route, state.probe);
    if (parts[0] === "settings" && parts[1] === "connections") {
      const id = parts[2];
      if (parts[3] === "test") {
        const item = state.connections.find((c) => c.id === id)!;
        item.test = { status: state.probe.ok ? "ok" : "failed", latency_ms: state.probe.latency_ms, code: state.probe.code, tested_at: now };
        return respond(route, state.probe);
      }
      if (method === "POST") {
        const created = connection(`conn-${state.connections.length + 1}`, (body as Json).model || "model", { ...(body as Json), api_key: undefined, has_key: Boolean((body as Json).api_key), test: { status: "untested", latency_ms: null, code: "", tested_at: null } });
        delete created.api_key;
        state.connections.push(created);
        return respond(route, created, 201);
      }
      if (method === "PUT") {
        const item = state.connections.find((c) => c.id === id)!;
        Object.assign(item, { ...(body as Json) });
        delete item.api_key;
        return respond(route, item);
      }
      if (method === "DELETE") {
        const inUse = state.defaults.search_llm === id || state.defaults.review_llm === id;
        const replacement = url.searchParams.get("replacement") || "";
        if (inUse && !replacement && state.connections.some((c) => c.id !== id && c.kind === "llm")) return respond(route, error("connection_in_use", 409), 409);
        state.connections = state.connections.filter((c) => c.id !== id);
        for (const key of ["search_llm", "review_llm"]) if (state.defaults[key] === id) state.defaults[key] = replacement;
        return respond(route, null, 204);
      }
    }
    if (parts[0] === "settings" && parts[1] === "sources") {
      const item = state.sourceSettings.find((s) => s.id === parts[2])!;
      if (parts[3] === "test") {
        item.test = { status: "ok", latency_ms: 290, code: "", tested_at: now };
        return respond(route, { ok: true, latency_ms: 290, code: "" });
      }
      const payload = body as Json;
      if (payload.api_key) { item.has_key = true; item.key_origin = "settings"; item.usable = true; }
      if (payload.options) item.options = { ...item.options, ...payload.options };
      return respond(route, null, 204);
    }

    if (path === "/history" && method === "GET") {
      const query = (url.searchParams.get("query") || "").toLowerCase();
      const items = historyItems(state).filter((item) => !query || item.question.toLowerCase().includes(query));
      return respond(route, { items, next_cursor: null, matched_count: query ? items.length : 0 });
    }
    if (parts[0] === "history" && parts[1]) {
      if (method === "DELETE") { delete state.runs[parts[1]]; state.deleted.push(parts[1]); return respond(route, { run_id: parts[1], deleted: true }); }
      state.runs[parts[1]].display_title = (body as Json).title;
      return respond(route, null, 204);
    }
    if (path === "/runs" && method === "POST") {
      state.submissions.push(body as Json);
      if (Object.values(state.runs).some((run) => ["queued", "running"].includes(run.status)))
        return respond(route, { error: { code: "active_run_exists", message: "", request_id: "t", details: { run_id: Object.values(state.runs).find((run) => ["queued", "running"].includes(run.status))!.id } } }, 409);
      const created = runDetail("abababab-abab-4bab-8bab-abababababab", (body as Json).question, "queued", { queue: { ahead: 2, running: 1 } });
      state.runs[created.id] = created;
      return respond(route, created, 202);
    }
    if (parts[0] === "runs" && parts[1]) {
      const run = state.runs[parts[1]];
      if (!run) return respond(route, error("run_not_found", 404), 404);
      if (parts.length === 2) return respond(route, run);
      if (parts[2] === "steps" && parts[4] === "retry") {
        if (!finished.has(String(run.status))) return respond(route, error("run_not_retryable", 409), 409);
        const step = parts[3];
        state.retries.push({ run_id: run.id, step });
        const logs = state.events[run.id] || [];
        let sequence = logs[logs.length - 1]?.sequence || 0;
        logs.push(event(run.id, (sequence += 1), "run.retry_requested", { code: step, from_status: run.status }, "lifecycle"));
        // The run is requeued; the worker claims it unless the spec holds the run
        // in the queue to observe that state.
        if (!state.retryManual) {
          logs.push(event(run.id, (sequence += 1), "run.claimed", { attempt: 2, lease_generation: 2 }, "lifecycle"));
          logs.push(event(run.id, (sequence += 1), "query.started", {}, "lifecycle"));
        }
        state.events[run.id] = logs;
        Object.assign(run, {
          status: state.retryManual ? "queued" : "running",
          error_code: "", warnings: [], result_count: 0, finished_at: null, last_event_sequence: sequence,
        });
        return respond(route, run, 202);
      }
      if (parts[2] === "events") {
        const after = Number(request.headers()["last-event-id"] || 0);
        const frames = (state.events[run.id] || []).filter((item) => item.sequence > after).map((item) => `id: ${item.sequence}\ndata: ${JSON.stringify(item)}\n\n`).join("");
        return route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: frames });
      }
      if (parts[2] === "audit") return respond(route, { query_portfolio: [], loop_decisions: [], candidate_queue: {}, candidate_lifecycle: {}, recovery: {} });
      if (parts[2] === "cancel") { run.status = "cancelled"; return respond(route, run, 202); }
      if (parts[2] === "results") {
        let list = [...(state.results[run.id] || [])];
        const query = (url.searchParams.get("query") || "").toLowerCase();
        if (query) list = list.filter((item) => item.title.toLowerCase().includes(query));
        const min = url.searchParams.get("min_score");
        if (min) list = list.filter((item) => item.score >= Number(min));
        if (url.searchParams.get("metadata") === "pdf") list = list.filter((item) => item.pdf_url);
        const cursor = url.searchParams.get("cursor");
        const page = cursor ? list.slice(4) : list.slice(0, 4);
        return respond(route, { results: page, next_cursor: !cursor && list.length > 4 ? "next" : null, total_count: state.results[run.id]?.length || 0,
          matched_count: list.length, scored_count: list.length, high_score_count: list.length, max_score: 10 });
      }
      if (parts[2] === "export-selected") return route.fulfill({ status: 200, headers: { "Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="selected.csv"' }, body: "uid\n" });
      if (parts[2] === "citation-map") return respond(route, {
        status: "completed", seed_count: 1, added_candidates: 2,
        nodes: [
          { id: "https://example.test/seed", title: state.seedTitle, roles: ["seed"], citations: 20, year: 2023, score: 9, reasoning: "Core." },
          { id: "https://example.test/result", title: "Result paper", roles: ["result"], citations: 5, year: 2024, score: 7 },
          { id: "https://example.test/forward", title: "Citing paper", roles: ["forward"], citations: 1 },
        ],
        edges: [{ source: "https://example.test/forward", target: "https://example.test/seed", type: "cites" }, { source: "https://example.test/seed", target: "https://example.test/result", type: "cites" }],
      });
    }

    if (path === "/bibliometrics" && method === "POST") {
      const created = analysis(state, (body as Json).source_run_id);
      state.analyses = [created];
      return respond(route, created, 201);
    }
    if (path === "/bibliometrics" && method === "GET")
      return respond(route, { items: state.analyses.map((item) => ({ id: item.id, title: item.title, paper_count: item.paper_count, scope: item.scope, source_run_id: item.source_run_id, created_at: item.created_at })), next_offset: null, total_count: state.analyses.length });
    if (parts[0] === "bibliometrics" && parts[1]) {
      const item = state.analyses.find((a) => a.id === parts[1]);
      if (!item) return respond(route, error("analysis_not_found", 404), 404);
      if (parts[2] === "network") return respond(route, {
        corpus_count: 642, eligible_node_count: 3, eligible_paper_count: 600, total_edge_count: 2, parameters: { ...(body as Json) },
        nodes: [{ id: "a", label: "systematic review", count: 148, group: 1 }, { id: "b", label: "screening", count: 90, group: 1 }, { id: "c", label: "llm", count: 40, group: 2 }],
        edges: [{ source: "a", target: "b", weight: 36, similarity: 0.33 }, { source: "a", target: "c", weight: 20, similarity: 0.2 }],
      });
      if (method === "DELETE") { state.analyses = []; return respond(route, null, 204); }
      return respond(route, item);
    }

    if (path === "/reviews" && method === "GET")
      return respond(route, { items: state.reviews.map((item) => ({ id: item.id, topic: item.content.topic, display_title: item.display_title, paper_count: item.content.papers.length,
        abstract_count: 2, section_count: item.content.outline?.sections.length || 0, completed_sections: item.document.sections.length, pending_sections: 0,
        phase: item.document.sections.length ? "complete" : item.content.outline ? "outline" : "draft", active: false, updated_at: now })), next_offset: null, total_count: state.reviews.length });
    if (path === "/reviews" && method === "POST") {
      const created = reviewDetail(state, body as Json);
      state.reviews = [created];
      return respond(route, created, 201);
    }
    if (parts[0] === "reviews" && parts[1]) {
      const item = state.reviews.find((r) => r.id === parts[1]);
      if (!item) return respond(route, error("review_not_found", 404), 404);
      if (parts[2] === "jobs") {
        withOutline(item);
        item.version += 1;
        item.latest_job = { ...runDetail("cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd", item.content.topic, "completed"), kind: "review" };
        return respond(route, item.latest_job, 202);
      }
      if (parts[2] === "outline") { item.content.outline = (body as Json).outline; item.version += 1; item.content.outline_revision += 1; return respond(route, item); }
      if (parts[2] === "versions") return respond(route, { items: [] });
      return respond(route, item);
    }
    return respond(route, error("not_mocked", 404), 404);
  });
  return state;
}

export function requestsTo(state: MockState, method: string, path: string | RegExp) {
  return state.requests.filter((item) => item.method === method && (typeof path === "string" ? item.path === path : path.test(item.path)));
}
