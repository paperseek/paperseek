import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { ApiError } from "../../api/client";
import { connectionById, connectionForRoute, loadSettings, workspaceSettings } from "../settings/store";
import { reviewApi, type CitationStyle, type JobRequest, type Review } from "./api";

export function useReview(id: Ref<string>, citationStyle: Ref<CitationStyle>) {
  const review = ref<Review | null>(null), error = ref(""), busy = ref(false), loading = ref(true);
  const operation = ref<{ jobId: string; action: JobRequest["action"]; title: string } | null>(null);
  // The saved LLM connection used for the next writing job.
  const connection = ref("");
  const active = computed(() => review.value?.active_job || null);
  let alive = true, generation = 0, stream: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let lastPayload = "", key = "", modelLoaded = false;
  const cursors = new Map<string, number>();
  // A failed background refresh clears itself on the next good one; an error from a user action stays.
  let refreshFailed = false;
  function report(e: unknown) { refreshFailed = false; error.value = e instanceof ApiError ? e.code : "reviewError"; }
  async function load() {
    const own = generation, target = id.value, style = citationStyle.value;
    const value = await reviewApi.get(target, style);
    if (!alive || own !== generation || style !== citationStyle.value) return;
    // Concurrent reads cannot roll back a newer server version.
    if (!review.value || value.version >= review.value.version) review.value = value;
    if (!modelLoaded) {
      await loadSettings();
      connection.value = connectionForRoute(value.content.model_route as Record<string, string> | undefined)
        || workspaceSettings.defaults?.review_llm || "";
      modelLoaded = true;
    }
  }
  // A connection that disappears (deleted, or another service mode) falls back to the default.
  watch(() => workspaceSettings.connections, () => {
    if (modelLoaded && workspaceSettings.loaded && !connectionById(connection.value)) connection.value = workspaceSettings.defaults?.review_llm || "";
  });
  async function refresh() {
    try {
      await load();
      if (refreshFailed) { refreshFailed = false; error.value = ""; }
    } catch (e) { report(e); refreshFailed = true; }
  }
  watch(citationStyle, refresh);
  function scheduleRefresh() {
    if (timer) return;
    timer = setTimeout(() => { timer = undefined; void refresh(); }, 300);
  }
  watch(() => active.value?.id, async (jobId) => {
    stream?.abort();
    if (!jobId) return;
    const controller = new AbortController(); stream = controller;
    const target = id.value;
    while (!controller.signal.aborted && alive) {
      try {
        await reviewApi.events(target, jobId, cursors.get(jobId) || 0, event => {
          cursors.set(jobId, Math.max(cursors.get(jobId) || 0, event.sequence)); scheduleRefresh();
        }, controller.signal);
      } catch { if (controller.signal.aborted) break; }
      await refresh();
      if (controller.signal.aborted || active.value?.id !== jobId) break;
      await new Promise<void>(resolve => {
        const done = () => { clearTimeout(wait); controller.signal.removeEventListener("abort", done); resolve(); };
        const wait = setTimeout(done, 1200); controller.signal.addEventListener("abort", done, { once: true });
      });
    }
  });
  watch(id, async (value) => {
    generation++; stream?.abort(); if (timer) clearTimeout(timer); timer = undefined;
    review.value = null; error.value = ""; refreshFailed = false; modelLoaded = false; operation.value = null;
    loading.value = Boolean(value);
    if (!value) return;
    try { await load(); } catch (e) { report(e); } finally { loading.value = false; }
  }, { immediate: true });
  async function action(operation: () => Promise<unknown>) {
    if (busy.value) return;
    busy.value = true; error.value = ""; refreshFailed = false;
    try { await operation(); await load(); } catch (e) { report(e); } finally { busy.value = false; }
  }
  async function submit(actionType: JobRequest["action"], extra: Partial<JobRequest> = {}) {
    const current = review.value; if (!current || active.value) return;
    const payload: JobRequest = { action: actionType, expected_version: current.version,
      outline_revision: current.content.outline_revision, connection_id: connection.value, ...extra };
    const signature = JSON.stringify(payload);
    if (signature !== lastPayload) { lastPayload = signature; key = crypto.randomUUID(); }
    await action(async () => {
      const job = await reviewApi.submit(current.id, payload, key);
      operation.value = { jobId: job.id, action: actionType, title: current.content.outline?.sections.find(section => section.id === extra.section_id)?.title || "" };
    });
  }
  async function cancel() {
    if (active.value) await action(() => reviewApi.cancel(id.value, active.value!.id));
  }
  onBeforeUnmount(() => {
    alive = false; stream?.abort(); if (timer) clearTimeout(timer);
  });
  return { review, connection, error, busy, loading, active, operation, action, submit, cancel, refresh, report };
}
