import type { RunEvent, RunStep } from "../../api/types";

/**
 * Retry boundaries in execution order, mirroring the backend's `RETRY_STEPS`.
 * Retrying one boundary also re-runs every boundary after it, while the
 * boundaries before it keep the checkpoints of the previous attempt.
 */
export const RETRY_ORDER: readonly RunStep[] = ["query", "retrieval", "citation", "selection", "ranking", "results"];

export function retryRank(step: RunStep): number {
  const index = RETRY_ORDER.indexOf(step);
  return index < 0 ? RETRY_ORDER.length : index;
}

/** The boundaries that re-run when the pipeline restarts at `step` (inclusive). */
export function boundariesFrom(step: RunStep): RunStep[] {
  return RETRY_ORDER.slice(retryRank(step));
}

export interface AttemptInfo {
  /** True once the run has been requeued at least once. */
  retried: boolean;
  /** The boundary the current attempt restarts at, if any. */
  step: RunStep | null;
  /** Events observed since the last requeue (every event for a first attempt). */
  events: RunEvent[];
}

function isRunStep(value: string): value is RunStep {
  return (RETRY_ORDER as readonly string[]).includes(value);
}

/** Read the retry boundary a run is currently re-executing from the event log. */
export function attemptInfo(events: RunEvent[]): AttemptInfo {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type !== "run.retry_requested") continue;
    const code = String((event.payload || {}).code || "");
    return { retried: true, step: isRunStep(code) ? code : null, events: events.slice(index + 1) };
  }
  return { retried: false, step: null, events };
}

/** Split the log into attempts; the last group is the one in progress. */
function attemptGroups(events: RunEvent[]): RunEvent[][] {
  const groups: RunEvent[][] = [[]];
  for (const event of events) {
    if (event.event_type === "run.retry_requested") groups.push([]);
    else groups[groups.length - 1].push(event);
  }
  return groups;
}

export interface ScopedEvents {
  /** Every matching event, whichever attempt produced it. */
  all: RunEvent[];
  /** The events that own the result currently on screen. */
  events: RunEvent[];
  /** `events` were kept from an earlier attempt (the boundary was not re-run). */
  reused: boolean;
  /** The boundary is queued to re-run and has not produced events yet. */
  waiting: boolean;
}

/**
 * Pick the events that describe the current state of one pipeline boundary.
 *
 * A retry drops the checkpoint of its own boundary and of everything that
 * consumes it, so boundaries upstream of the retry keep their previous
 * attempt's events, while the retry and the boundaries after it wait for the
 * new attempt to produce fresh ones.
 */
export function scopedEvents(
  allEvents: RunEvent[],
  matches: (event: RunEvent) => boolean,
  boundary: RunStep,
  attempt: AttemptInfo,
): ScopedEvents {
  const all = allEvents.filter(matches);
  const groups = attemptGroups(allEvents);
  const current = groups[groups.length - 1].filter(matches);
  if (current.length) return { all, events: current, reused: false, waiting: false };
  if (attempt.step && retryRank(boundary) < retryRank(attempt.step)) {
    for (let index = groups.length - 2; index >= 0; index -= 1) {
      const found = groups[index].filter(matches);
      if (found.length) return { all, events: found, reused: true, waiting: false };
    }
  }
  return { all, events: [], reused: false, waiting: attempt.step !== null };
}

/** The events of one event family (`prefix`) that belong to a boundary. */
export function familyEvents(
  allEvents: RunEvent[],
  prefix: string,
  boundary: RunStep,
  attempt: AttemptInfo,
): ScopedEvents {
  return scopedEvents(allEvents, (event) => event.event_type.startsWith(prefix), boundary, attempt);
}

/** The newest payload of a family inside an already scoped event list. */
export function lastPayload(events: RunEvent[], prefix: string): Record<string, unknown> | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type.startsWith(prefix)) return event.payload || {};
  }
  return null;
}
