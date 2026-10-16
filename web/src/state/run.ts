import { reactive } from "vue";

import { api, ApiError } from "../api/client";
import type { RunAudit, RunDetail, RunEvent, RunStep, SearchSubmission } from "../api/types";

export type { RunStep };
export type StageKey = "query" | "search" | "ranking" | "results";
export type StageState = "idle" | "active" | "complete" | "failed";
export type ConnectionState = "connected" | "reconnecting" | "offline";

const state = reactive({
  run: null as RunDetail | null,
  events: [] as RunEvent[],
  audit: null as RunAudit | null,
  lastEventSequence: 0,
  connection: "offline" as ConnectionState,
  cancelPending: false,
  // The pipeline step whose retry request is in flight, if any.
  retryPending: "" as RunStep | "",
  // The step a retry was accepted for, until the worker picks the run up again.
  retryNotice: "" as RunStep | "",
  stages: {
    query: "idle",
    search: "idle",
    ranking: "idle",
    results: "idle",
  } as Record<StageKey, StageState>,
  // First durable event time per stage; used only to derive elapsed time (§6).
  stageStartedAt: {
    query: null,
    search: null,
    ranking: null,
    results: null,
  } as Record<StageKey, string | null>,
  error: "",
});

const terminalStatuses = new Set(["completed", "completed_with_warnings", "failed", "cancelled"]);
let recoveryGeneration = 0;
let recoveryController: AbortController | null = null;

class EventGapError extends Error {}

export function eventStage(event: { event_type: string }): StageKey | null {
  const type = event.event_type;
  if (type === "query.started" || type === "query.planned") return "query";
  if (type.startsWith("query.")) return "search";
  if (/^(retrieval|citation|selection|ranking)\./.test(type)) return "ranking";
  if (/^(final\.|workflow\.completed)/.test(type)) return "results";
  return null;
}

const stageOrder: StageKey[] = ["query", "search", "ranking", "results"];

/** Drop everything derived from the event stream, keeping the run identity. */
function resetDerivedState(): void {
  state.events = [];
  state.audit = null;
  state.lastEventSequence = 0;
  state.error = "";
  state.cancelPending = false;
  state.retryNotice = "";
  for (const key of stageOrder) {
    state.stages[key] = "idle";
    state.stageStartedAt[key] = null;
  }
}

/** The requeued run is running again, so the retry banner has done its job. */
function clearRetryNotice(): void {
  state.retryNotice = "";
  state.retryPending = "";
}

function resetRunState(): void {
  state.run = null;
  state.retryPending = "";
  resetDerivedState();
}

function observe(runId: string, event: RunEvent): void {
  if (event.run_id !== runId) return;
  if (event.sequence <= state.lastEventSequence) return;
  if (event.sequence !== state.lastEventSequence + 1) throw new EventGapError("event_sequence_gap");
  state.events.push(event);
  state.lastEventSequence = event.sequence;
  applyLiveAudit(event);
  // A retry requeues the same run: replaying the stream clears the previous
  // attempt's outcome before the new attempt's events rebuild the stages.
  if (event.event_type === "run.retry_requested") {
    state.run = { ...state.run!, status: "queued", current_step: "queued", error_code: "", warnings: [] };
    for (const key of stageOrder) {
      state.stages[key] = "idle";
      state.stageStartedAt[key] = null;
    }
    // The audit is rebuilt from the new attempt; the query checkpoints it came
    // from are kept, so a completed run refills it from the server instead.
    if (state.audit) state.audit = { ...state.audit, loop_decisions: [], query_portfolio: [] };
    return;
  }
  if (['run.claimed', 'run.started'].includes(event.event_type) && state.run?.status === 'queued') {
    state.run = { ...state.run, status: 'running', queue: null };
    clearRetryNotice();
  }
  if (event.event_type === "run.cancelling" && state.run?.status !== "cancelled") {
    state.run = { ...state.run!, status: "cancelling", current_step: "cancelling" };
  }
  if (event.event_type === "run.cancelled") {
    state.run = { ...state.run!, status: "cancelled", current_step: "cancelled" };
    applyTerminalFailure();
    return;
  }
  if (state.run?.status === "cancelling" || state.run?.status === "cancelled") return;
  const stage = eventStage(event);
  if (stage) {
    if (!state.stageStartedAt[stage] && event.occurred_at) state.stageStartedAt[stage] = event.occurred_at;
    const index = stageOrder.indexOf(stage);
    for (const previous of stageOrder.slice(0, index)) {
      if (state.stages[previous] !== "failed") state.stages[previous] = "complete";
    }
    const failed = /failed|error/.test(`${event.event_type} ${event.stage}`);
    state.stages[stage] = failed ? "failed" : "active";
  }
  if (event.event_type === "run.completed" || event.event_type === "run.completed_with_warnings"
      || ["completed", "completed_with_warnings"].includes(state.run?.status || "")) {
    for (const key of Object.keys(state.stages) as StageKey[]) state.stages[key] = "complete";
  }
}

function applyLiveAudit(event: RunEvent): void {
  const audit = state.audit || {
    query_portfolio: [],
    loop_decisions: [],
    candidate_queue: {},
    candidate_lifecycle: {},
    recovery: {},
  };
  if (event.event_type === "query.progress" || event.event_type === "query.refinement_stopped") {
    audit.loop_decisions = [...(audit.loop_decisions || []), { ...event.payload }];
  } else if (event.event_type === "query.portfolio_recorded") {
    audit.query_portfolio = [...(audit.query_portfolio || []), { ...event.payload }];
  } else if (event.event_type === "workflow.recovery") {
    const recovery = Object.fromEntries(
      Object.entries(event.payload || {}).map(([key, value]) => [key, Number(value) || 0]),
    );
    recovery.total = Object.values(recovery).reduce((total, value) => total + value, 0);
    audit.recovery = recovery;
  }
  state.audit = audit;
}

function isCurrent(runId: string, generation: number): boolean {
  return recoveryGeneration === generation && state.run?.id === runId;
}

function isCurrentGeneration(generation: number): boolean {
  return recoveryGeneration === generation;
}

function applySnapshot(runId: string, generation: number, snapshot: RunDetail): boolean {
  if (!isCurrent(runId, generation) || snapshot.id !== runId) return false;
  const currentStatus = state.run?.status;
  if (currentStatus === "cancelled" && snapshot.status !== "cancelled") return true;
  if (currentStatus === "cancelling" && !["cancelling", "cancelled"].includes(snapshot.status)) return true;
  state.run = snapshot;
  if (snapshot.status !== "queued") clearRetryNotice();
  if (["completed", "completed_with_warnings"].includes(snapshot.status)) {
    for (const key of stageOrder) state.stages[key] = "complete";
  }
  applyTerminalFailure();
  return true;
}

function applyTerminalFailure(): void {
  if (state.run?.status !== "failed" && state.run?.status !== "cancelled") return;
  for (const key of stageOrder) {
    if (state.stages[key] === "active") state.stages[key] = state.run.status === "cancelled" ? "idle" : "failed";
  }
}

export async function submitSearch(payload: SearchSubmission): Promise<RunDetail> {
  stopRunRecovery();
  resetRunState();
  const submitted = await api.submit(payload, globalThis.crypto.randomUUID());
  state.run = submitted;
  state.stages.query = submitted.status === 'queued' ? 'idle' : 'active';
  return submitted;
}

function delay(ms: number, signal?: AbortSignal | null): Promise<void> {
  if (signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal?.addEventListener("abort", done, { once: true });
  });
}

// Offline pauses reconnect churn; `online` resumes with a fresh snapshot first (§10.6).
function waitForOnline(signal?: AbortSignal | null): Promise<void> {
  if (signal?.aborted || globalThis.navigator?.onLine !== false) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      globalThis.removeEventListener("online", done);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    globalThis.addEventListener("online", done, { once: true });
    signal?.addEventListener("abort", done, { once: true });
  });
}

export async function recoverRun(runId: string, loadAudit: typeof api.audit | null = api.audit): Promise<void> {
  stopRunRecovery();
  const generation = recoveryGeneration;
  const controller = new AbortController();
  recoveryController = controller;
  // Recovering the run that is already on screen (e.g. right after a 202 accept)
  // revalidates in place so the workbench does not flash back to a placeholder.
  if (state.run?.id === runId) {
    state.error = "";
    state.cancelPending = false;
  } else {
    resetRunState();
  }
  while (isCurrentGeneration(generation) && !controller.signal.aborted) {
    try {
      const snapshot = await api.run(runId, controller.signal);
      if (!isCurrentGeneration(generation) || snapshot.id !== runId) return;
      if (state.run?.id !== runId) state.run = snapshot;
      else if (!applySnapshot(runId, generation, snapshot)) return;
      state.connection = "connected";
      await api.events(
        runId,
        state.lastEventSequence,
        (event) => {
          if (isCurrent(runId, generation)) observe(runId, event);
        },
        controller.signal,
      );
      if (!isCurrent(runId, generation)) return;
      const refreshedSnapshot = await api.run(runId, controller.signal);
      if (!applySnapshot(runId, generation, refreshedSnapshot)) return;
      if (terminalStatuses.has(state.run.status)) {
        if (loadAudit && ["completed", "completed_with_warnings"].includes(state.run.status)) {
          const audit = await loadAudit(runId, controller.signal);
          if (!isCurrent(runId, generation)) return;
          state.audit = audit;
        }
        state.connection = "offline";
        return;
      }
    } catch (error) {
      if (controller.signal.aborted || !isCurrentGeneration(generation)) return;
      if (error instanceof ApiError && error.status < 500 && ![408, 429].includes(error.status)) {
        state.connection = "offline";
        throw error;
      }
      if (error instanceof EventGapError) continue;
    }
    if (globalThis.navigator?.onLine === false) {
      state.connection = "offline";
      await waitForOnline(controller.signal);
    } else {
      state.connection = "reconnecting";
      await delay(800, controller.signal);
    }
  }
}

export async function cancelRun(runId: string): Promise<void> {
  if (state.run?.id !== runId || !["queued", "running"].includes(state.run.status)) return;
  const generation = recoveryGeneration;
  state.cancelPending = true;
  state.error = "";
  try {
    const cancelled = await api.cancel(runId, recoveryController?.signal);
    applySnapshot(runId, generation, cancelled);
  } finally {
    if (isCurrent(runId, generation)) state.cancelPending = false;
  }
}

/**
 * Requeue a finished run at one pipeline step. The run replays its whole event
 * history, so the derived state is rebuilt from a clean slate.
 */
export async function retryStep(runId: string, step: RunStep): Promise<void> {
  if (state.run?.id !== runId || state.retryPending) return;
  state.retryPending = step;
  state.error = "";
  let accepted = false;
  try {
    const retried = await api.retryStep(runId, step);
    stopRunRecovery();
    resetDerivedState();
    state.run = retried;
    // The run is queued until the worker claims it; the banner survives that.
    state.retryPending = step;
    state.retryNotice = step;
    accepted = true;
  } catch (error) {
    state.error = error instanceof Error ? error.message : "run_unavailable";
  } finally {
    if (!accepted && state.retryPending === step) state.retryPending = "";
  }
  if (!accepted) return;
  try {
    await recoverRun(runId);
  } catch (error) {
    if (!state.error) state.error = error instanceof Error ? error.message : "run_unavailable";
  }
}

export function stopRunRecovery(): void {
  recoveryGeneration += 1;
  recoveryController?.abort();
  recoveryController = null;
  state.connection = "offline";
  state.cancelPending = false;
}

/** Remove the previous identity's data and invalidate every in-flight response. */
export function clearRun(): void {
  stopRunRecovery();
  resetRunState();
}

export const runState = state;
