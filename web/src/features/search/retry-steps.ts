import type { RunEvent, RunStep } from "../../api/types";
import type { UiKey } from "../../locales/ui";
import type { StageKey } from "../../state/run";
import { boundariesFrom } from "./run-attempt";

/**
 * Which pipeline boundary re-runs the work a stage shows. The UI groups the
 * pipeline differently from the backend: "search" is the query iteration loop
 * and "ranking" is recall plus ranking, so a stage maps to the boundary that
 * owns its events.
 */
export const STAGE_RETRY_STEP: Record<StageKey, RunStep> = {
  query: "query",
  search: "query",
  ranking: "ranking",
  results: "results",
};

/** The boundary behind each ranking sub-step row. */
export const SUBSTEP_RETRY_STEP: Record<string, RunStep> = {
  stepMultiLane: "retrieval",
  stepEmbedding: "retrieval",
  stepCitation: "citation",
  stepIncrementalEmbedding: "selection",
  stepRerank: "selection",
  stepDecision: "selection",
  stepCandidateSelection: "selection",
  stepLlmRanking: "ranking",
};

/** Display name of each pipeline boundary, so a retry says what it re-runs. */
export const BOUNDARY_LABEL: Record<RunStep, UiKey> = {
  query: "boundaryQuery",
  retrieval: "boundaryRetrieval",
  citation: "boundaryCitation",
  selection: "boundarySelection",
  ranking: "boundaryRanking",
  results: "boundaryResults",
};

type UiText = (key: UiKey, values?: Record<string, string | number>) => string;

/** True for the durable events that mark a step as failed or degraded. */
export function isStepTrouble(event: RunEvent): boolean {
  if (/\.(failed|degraded)$/.test(event.event_type)) return true;
  return String((event.payload || {}).status || "") === "degraded";
}

/** Map an event type to the boundary that produced it, mirroring the backend. */
export function retryStepForEventType(eventType: string): RunStep | null {
  if (eventType.startsWith("query.")) return "query";
  if (eventType.startsWith("retrieval.")) return "retrieval";
  if (eventType.startsWith("citation.")) return "citation";
  if (eventType.startsWith("selection.")) return "selection";
  if (eventType.startsWith("ranking.")) return "ranking";
  if (eventType.startsWith("final.") || eventType.startsWith("workflow.completed")) return "results";
  return null;
}

/**
 * The boundary to re-run for a stage: the last failed or degraded event decides,
 * so a query problem retries the query while a retrieval problem retries only
 * the retrieval. Falls back to the stage's own boundary.
 */
export function stageRetryStep(stage: StageKey, events: RunEvent[]): RunStep {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!isStepTrouble(event)) continue;
    const step = retryStepForEventType(event.event_type);
    if (step) return step;
  }
  return STAGE_RETRY_STEP[stage];
}

export function boundaryLabel(u: UiText, step: RunStep): string {
  return u(BOUNDARY_LABEL[step]);
}

/** "Rerun from ⟨boundary⟩": names the exact step a retry control restarts. */
export function retryLabel(u: UiText, step: RunStep): string {
  return u("retryFromStep", { step: boundaryLabel(u, step) });
}

/** "Also re-runs: ⟨boundary⟩ → …": the downstream steps a retry drags along. */
export function retryScopeLabel(u: UiText, step: RunStep): string {
  const steps = boundariesFrom(step).map((item) => boundaryLabel(u, item)).join(" → ");
  return u("retryScope", { steps });
}
