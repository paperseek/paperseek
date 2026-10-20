import { download, json, streamEvents } from "../../api/client";
import type { components } from "../../api/generated";
import type { RunDetail, RunEvent } from "../../api/types";

type Schema = components["schemas"];
export const MAX_REVIEW_PAPERS = 256;
export type Review = Schema["ReviewDetail"];
export type Outline = Schema["ReviewOutline"];
export type Section = Schema["WrittenSection"];
export type Body = Schema["SectionBody"];
export type Citation = Schema["ReviewCitation"];
export type CitationStyle = NonNullable<Schema["RenderedReview"]["citation_style"]>;
export type JobRequest = Schema["ReviewJobRequest"];
export type Paper = Schema["ReviewPaper"];
export type Revision = Schema["ReviewRevision"];
export type ReviewPage = Schema["ReviewPage"];
const root = "/api/v1/reviews";
const send = (method: string, data: unknown, key?: string): RequestInit => ({
  method, headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) }, body: JSON.stringify(data),
});

export const reviewApi = {
  create: (data: Schema["ReviewCreate"], key: string) => json<Review>(root, send("POST", data, key)),
  list: (offset = 0, query = "", signal?: AbortSignal) => json<ReviewPage>(`${root}?${new URLSearchParams({ offset: String(offset), query })}`, { signal }),
  rename: (id: string, title: string) => json<void>(`${root}/${id}`, send("PATCH", { title })),
  get: (id: string, style: CitationStyle = "numeric") => json<Review>(`${root}/${id}?citation_style=${style}`),
  outline: (id: string, data: Schema["OutlineSave"]) => json<Review>(`${root}/${id}/outline`, send("PUT", data)),
  section: (id: string, part: string, data: Schema["SectionSave"]) => json<Review>(`${root}/${id}/sections/${part}`, send("PUT", data)),
  versions: (id: string, part: string) => json<Schema["ReviewVersions"]>(`${root}/${id}/versions/${part}`),
  suggest: (data: Schema["PaperSuggestionRequest"]) => json<Schema["PaperSuggestions"]>(`${root}/paper-suggestions`, send("POST", data)),
  submit: (id: string, data: JobRequest, key: string) => json<RunDetail>(`${root}/${id}/jobs`, send("POST", data, key)),
  cancel: (id: string, job: string) => json<RunDetail>(`${root}/${id}/jobs/${job}/cancel`, { method: "POST" }),
  events: (id: string, job: string, after: number, onEvent: (event: RunEvent) => void, signal: AbortSignal) =>
    streamEvents(`${root}/${id}/jobs/${job}/events`, after, onEvent, signal),
  remove: (id: string, version: number) => json<void>(`${root}/${id}?version=${version}`, { method: "DELETE" }),
  export: (id: string, version: number, format: "md" | "docx" | "bibtex", style: CitationStyle = "numeric") =>
    download(`${root}/${id}/export?version=${version}&format=${format}&citation_style=${style}`, `review.${format}`),
};

export function saveDownload(file: { blob: Blob; filename: string }) {
  const url = URL.createObjectURL(file.blob);
  const link = document.createElement("a");
  link.href = url; link.download = file.filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function paperUrl(value: string): string | undefined {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : undefined; }
  catch { return undefined; }
}
