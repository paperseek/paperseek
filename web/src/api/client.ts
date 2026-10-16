import type {
  CitationMap,
  DiagnosticResult,
  DiagnosticSubmission,
  ErrorResponse,
  DeletionReceipt,
  HistoryPage,
  PaperResult,
  ResultPage,
  RunDetail,
  RunAudit,
  RunEvent,
  RunStep,
  SearchSubmission,
  SourceDescriptor,
} from "./types";

export interface ResultPageQuery {
  cursor?: string | null;
  query?: string;
  minScore?: number | null;
  metadata?: "abstract" | "doi" | "pdf" | "";
  sort?: "rank" | "score" | "citations" | "year";
}

export interface HistoryQuery {
  pageSize?: number;
  cursor?: string | null;
  query?: string;
}

export interface DownloadedFile {
  blob: Blob;
  filename: string;
}

const apiBase = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
let request: typeof fetch = (input, init) => fetch(input, init);

// Hosted supplies its session-aware transport at application startup.
export function setRequestHandler(handler: typeof fetch): void {
  request = handler;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: NonNullable<ErrorResponse["error"]["details"]>;

  constructor(status: number, code: string, details: NonNullable<ErrorResponse["error"]["details"]> = {}) {
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(`${apiBase}${path}`, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const code = payload?.error?.code || `http_${response.status}`;
    throw new ApiError(response.status, code, payload?.error?.details);
  }
  return response.status === 204 ? null as T : response.json() as Promise<T>;
}

export const api = {
  async sources(): Promise<SourceDescriptor[]> {
    return (await json<{ sources: SourceDescriptor[] }>("/api/v1/sources")).sources;
  },

  diagnostics(payload: DiagnosticSubmission): Promise<DiagnosticResult> {
    return json<DiagnosticResult>("/api/v1/diagnostics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  submit(payload: SearchSubmission, idempotencyKey: string): Promise<RunDetail> {
    return json<RunDetail>("/api/v1/runs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(payload),
    });
  },

  run(runId: string, signal?: AbortSignal): Promise<RunDetail> {
    const timeout = AbortSignal.timeout(15_000);
    return json<RunDetail>(`/api/v1/runs/${runId}`, { signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
  },

  cancel(runId: string, signal?: AbortSignal): Promise<RunDetail> {
    return json<RunDetail>(`/api/v1/runs/${runId}/cancel`, { method: "POST", signal });
  },

  /** Requeue a finished run so one pipeline step (and its dependents) runs again. */
  retryStep(runId: string, step: RunStep): Promise<RunDetail> {
    return json<RunDetail>(`/api/v1/runs/${runId}/steps/${step}/retry`, { method: "POST" });
  },

  resultPage(runId: string, options: ResultPageQuery = {}, signal?: AbortSignal): Promise<ResultPage> {
    const params = new URLSearchParams();
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.query) params.set("query", options.query);
    if (options.minScore !== undefined && options.minScore !== null) params.set("min_score", String(options.minScore));
    if (options.metadata) params.set("metadata", options.metadata);
    if (options.sort && options.sort !== "rank") params.set("sort", options.sort);
    const suffix = params.size ? `?${params.toString()}` : "";
    return json<ResultPage>(`/api/v1/runs/${runId}/results${suffix}`, { signal });
  },

  async results(runId: string, signal?: AbortSignal): Promise<PaperResult[]> {
    return (await this.resultPage(runId, {}, signal)).results || [];
  },

  citationMap(runId: string, signal?: AbortSignal): Promise<CitationMap> {
    return json<CitationMap>(`/api/v1/runs/${runId}/citation-map`, { signal });
  },

  audit(runId: string, signal?: AbortSignal): Promise<RunAudit> {
    return json<RunAudit>(`/api/v1/runs/${runId}/audit`, { signal });
  },

  async history(options: HistoryQuery = {}, signal?: AbortSignal): Promise<HistoryPage> {
    const params = new URLSearchParams();
    if (options.pageSize) params.set("page_size", String(options.pageSize));
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.query) params.set("query", options.query);
    const suffix = params.size ? `?${params.toString()}` : "";
    return json<HistoryPage>(`/api/v1/history${suffix}`, { signal });
  },

  async exportSelected(runId: string, format: "csv" | "json", uids: string[], signal?: AbortSignal): Promise<DownloadedFile> {
    const response = await request(`${apiBase}/api/v1/runs/${runId}/export-selected`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format, uids }),
      signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new ApiError(response.status, payload?.error?.code || `http_${response.status}`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/.exec(disposition);
    return { blob: await response.blob(), filename: match?.[1] || `export-selected.${format}` };
  },

  renameHistory(runId: string, title: string): Promise<void> {
    return json<void>(`/api/v1/history/${runId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) });
  },

  deleteHistoryRun(runId: string, signal?: AbortSignal): Promise<DeletionReceipt> {
    return json<DeletionReceipt>(`/api/v1/history/${runId}`, { method: "DELETE", signal });
  },

  exportUrl(runId: string, format: "csv" | "json"): string {
    return `${apiBase}/api/v1/runs/${runId}/export?format=${format}`;
  },

  logUrl(runId: string): string {
    return `${apiBase}/api/v1/runs/${runId}/logs`;
  },

  events(runId: string, afterSequence: number, onEvent: (event: RunEvent) => void, signal?: AbortSignal): Promise<void> {
    return streamEvents(`/api/v1/runs/${runId}/events`, afterSequence, onEvent, signal);
  },
};

export async function streamEvents(
    path: string,
    afterSequence: number,
    onEvent: (event: RunEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const idle = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const arm = (milliseconds: number) => {
      clearTimeout(timer);
      timer = setTimeout(() => idle.abort(new DOMException("Event stream timed out.", "TimeoutError")), milliseconds);
    };
    arm(15_000);
    try {
      const response = await request(`${apiBase}${path}`, {
        headers: {
          Accept: "text/event-stream",
          "Last-Event-ID": String(afterSequence),
        },
        signal: signal ? AbortSignal.any([signal, idle.signal]) : idle.signal,
      });
      if (!response.ok) throw new ApiError(response.status, `events_http_${response.status}`);
      if (!response.body) throw new Error("events_body_missing");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          // Both events and the server's 10-second keepalives prove this stream is alive.
          arm(30_000);
          const { done, value } = await reader.read();
          buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() || "";
          for (const frame of frames) {
            const lines = frame.split(/\r?\n/);
            const id = lines.find((line) => line.startsWith("id:"))?.slice(3).trim();
            const data = lines
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trimStart())
              .join("\n");
            if (data) {
              const event = JSON.parse(data) as RunEvent;
              if (id !== undefined && Number(id) !== event.sequence) throw new Error("event_cursor_mismatch");
              onEvent(event);
            }
          }
          if (done) break;
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
    } finally {
      clearTimeout(timer!);
      idle.abort();
    }
  }

export async function download(path: string, fallback: string): Promise<DownloadedFile> {
  const response = await request(`${apiBase}${path}`);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(response.status, payload?.error?.code || `http_${response.status}`);
  }
  const match = /filename="?([^";]+)"?/.exec(response.headers.get("Content-Disposition") || "");
  return { blob: await response.blob(), filename: match?.[1] || fallback };
}
