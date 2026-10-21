import { reactive } from "vue";
import { api, ApiError } from "../../api/client";
import type { PaperResult } from "../../api/types";
import { reviewApi, type Review } from "./api";

export type DraftPaper = PaperResult & { uid: string };
export type SizeChoice = "auto" | "brief" | "rich" | "custom";
const defaults = (language: "zh" | "en") => ({
  topic: "", language, preset: "standard" as "short" | "standard" | "detailed" | "custom", custom: 4,
  paragraphs: "auto" as SizeChoice, paragraphsCustom: 4, size: "auto" as SizeChoice,
  sizeCustom: language === "zh" ? 300 : 150, connection: "",
});

// One draft for this browser tab. It outlives route components, but never an account change.
export const reviewDraft = reactive({
  revision: 0, initialized: false, step: 1 as 1 | 2 | 3,
  source: null as { id: string; title: string; count: number } | null,
  papers: [] as DraftPaper[], selected: new Map<string, DraftPaper>(), reasons: new Map<string, string>(),
  cursor: null as string | null, query: "", form: defaults("zh"), moreOpen: false,
  created: null as Review | null, loadedFor: "", busy: false, error: "", suggesting: false,
  selection: "idle" as "idle" | "running" | "ready" | "failed",
  createKey: "", createSignature: "", jobKey: "", jobSignature: "",
});
let selectionController: AbortController | undefined;

export function initializeReviewDraft(language: "zh" | "en") {
  if (reviewDraft.initialized) return;
  Object.assign(reviewDraft.form, defaults(language));
  reviewDraft.initialized = true;
}

export function cancelReviewSelection() {
  selectionController?.abort();
  selectionController = undefined;
  reviewDraft.suggesting = false;
  reviewDraft.selection = reviewDraft.selected.size ? "ready" : "idle";
}

export function resetReviewDraft() {
  cancelReviewSelection();
  reviewDraft.revision++;
  Object.assign(reviewDraft, {
    initialized: false, step: 1, source: null, papers: [], cursor: null, query: "", moreOpen: false,
    created: null, loadedFor: "", busy: false, error: "", selection: "idle",
    createKey: "", createSignature: "", jobKey: "", jobSignature: "",
  });
  reviewDraft.selected.clear();
  reviewDraft.reasons.clear();
  Object.assign(reviewDraft.form, defaults("zh"));
}

export function abstract(paper: PaperResult) {
  const text = (new DOMParser().parseFromString(paper.abstract || "", "text/html").body.textContent || "").replace(/\s+/g, " ").trim();
  return /^(?:n\/?a|none|null|no abstract(?: available)?|abstract not available|how to cite|暂无摘要|无摘要|摘要缺失)?[ .。:：]*$/i.test(text) ? "" : text;
}
export const usable = (items: PaperResult[] = []) => items.filter((paper): paper is DraftPaper => Boolean(paper.uid));

export async function selectReviewPapers(count: number) {
  const draft = reviewDraft;
  if (!draft.source || draft.suggesting || !draft.form.topic.trim() || !draft.form.connection) return;
  const controller = new AbortController();
  selectionController = controller;
  const revision = draft.revision;
  const id = draft.source.id;
  const current = () => !controller.signal.aborted && revision === draft.revision && draft.source?.id === id;
  draft.suggesting = true;
  draft.selection = "running";
  draft.error = "";
  try {
    const { items } = await reviewApi.suggest({ source_run_id: id, topic: draft.form.topic.trim(),
      language: draft.form.language, count, connection_id: draft.form.connection }, controller.signal);
    if (!current()) return;
    const wanted = new Set(items.map(item => item.uid));
    const found = new Map(draft.papers.filter(paper => wanted.has(paper.uid)).map(paper => [paper.uid, paper]));
    let next: string | undefined;
    while (found.size < wanted.size) {
      const page = await api.resultPage(id, { cursor: next }, controller.signal);
      if (!current()) return;
      for (const paper of usable(page.results)) if (wanted.has(paper.uid)) found.set(paper.uid, paper);
      next = page.next_cursor || undefined;
      if (!next) break;
    }
    draft.selected.clear();
    draft.reasons.clear();
    for (const item of items) {
      const paper = found.get(item.uid);
      if (!paper || !abstract(paper)) continue;
      draft.selected.set(item.uid, paper);
      if (item.reason) draft.reasons.set(item.uid, [item.theme, item.reason].filter(Boolean).join(" · "));
    }
    draft.selection = "ready";
  } catch (e) {
    if (current()) {
      draft.error = e instanceof ApiError ? e.code : "reviewError";
      draft.selection = "failed";
    }
  } finally {
    if (current()) {
      draft.suggesting = false;
      selectionController = undefined;
    }
  }
}
