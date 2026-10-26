import type { RunEvent } from "../../api/types";
export { eventStage } from "../../state/run";

export interface StructuredRow {
  sequence: number;
  label: string;
  detail: string;
}

type TFunc = (key: string) => string;

export function eventLabel(t: TFunc, eventType: string): string {
  const labels: Record<string, string> = {
    "query.started": "eventQueryStarted",
    "query.planned": "eventQueryPlanned",
    "query.progress": "eventQueryProgress",
    "query.selected": "selectedQuery",
    "query.refinement_stopped": "eventQueryRefinementStopped",
    "query.portfolio_recorded": "eventQueryPortfolio",
    "retrieval.started": "eventRetrievalStarted",
    "retrieval.progress": "eventRetrievalProgress",
    "retrieval.completed": "eventRetrievalCompleted",
    "retrieval.degraded": "eventRetrievalDegraded",
    "retrieval.failed": "eventRetrievalFailed",
    "citation.started": "eventCitationStarted",
    "citation.progress": "eventCitationProgress",
    "citation.completed": "eventCitationCompleted",
    "citation.skipped": "eventCitationSkipped",
    "citation.degraded": "eventCitationDegraded",
    "ranking.started": "eventRankingStarted",
    "ranking.batch_completed": "eventRankingProgress",
    "ranking.reasons_progress": "eventRankingReasons",
    "ranking.completed": "eventRankingCompleted",
    "ranking.degraded": "eventRankingDegraded",
    "ranking.failed": "eventRankingFailed",
    "workflow.completed": "eventWorkflowCompleted",
    "workflow.recovery": "eventRecoveryApplied",
    "enrichment.completed": "eventEnrichmentCompleted",
    "enrichment.failed": "eventEnrichmentFailed",
  };
  return t(labels[eventType] || "progressUpdated");
}

export function localizedDecision(t: TFunc, value: unknown): string {
  const key = `decision_${String(value || "recorded")}`;
  const localized = t(key);
  return localized === key ? t("decisionRecorded") : localized;
}

export function localizedRefinementStop(t: TFunc, code: unknown): string {
  return t(code === "query_repeated" || code === "query_no_progress" ? code : "eventQueryRefinementStopped");
}

export function localizedFailure(t: TFunc, code: unknown): string {
  const key = String(code || "operation_failed");
  const localized = t(key);
  return localized === key ? t("operationFailed") : localized;
}

export function eventDetail(t: TFunc, event: { event_type: string; payload?: Record<string, unknown> }): string {
  const payload = event.payload || {};
  if (event.event_type === "query.started") {
    return payload.target_min === undefined ? t("progressStarted") : `${t("targetMinimum")}: ${Number(payload.target_min)}`;
  }
  if (event.event_type === "query.planned") {
    return [payload.intent, payload.current_query].filter(Boolean).map(String).join(" · ");
  }
  if (event.event_type === "query.progress") {
    return `#${Number(payload.iteration ?? 0)} · ${localizedDecision(t, payload.decision)} · ${t("retrievedPapers")} ${Number(payload.hit_count ?? 0)} · ${t("sourceTotal")} ${Number(payload.total ?? 0)}`;
  }
  if (event.event_type === "query.selected") {
    return t("querySelectionReason").replace("{n}", String(payload.selected_iteration));
  }
  if (event.event_type === "query.refinement_stopped") {
    return localizedRefinementStop(t, payload.code);
  }
  if (event.event_type === "query.portfolio_recorded") {
    return `${String(payload.query || payload.view_id || "")} · ${t("retrievedPapers")} ${Number(payload.hit_count ?? 0)}`;
  }
  if (event.event_type.startsWith("retrieval.")) {
    if (/failed/.test(event.event_type)) return localizedFailure(t, payload.code);
    if (/degraded/.test(event.event_type)) return t("degraded");
    const current = Number(payload.completed_lanes ?? 0);
    const total = Number(payload.total_lanes ?? (Array.isArray(payload.lanes) ? payload.lanes.length : 0));
    const loaded = Number(payload.candidate_count ?? 0);
    const fused = Number(payload.total_candidates ?? loaded);
    return `${t("retrievalLanes")} ${current} / ${total} · ${t("loadedCandidates")} ${loaded} · ${t("fusedCandidates")} ${fused}`;
  }
  if (event.event_type.startsWith("citation.")) {
    if (/skipped/.test(event.event_type)) return t("not_requested");
    if (/degraded/.test(event.event_type)) return t("degraded");
    return `${t("processedSeeds")} ${Number(payload.processed_seeds ?? 0)} / ${Number(payload.seed_count ?? 0)} · ${t("candidates")} ${Number(payload.added_candidates ?? payload.candidate_count ?? 0)}`;
  }
  if (event.event_type.startsWith("ranking.")) {
    if (/failed/.test(event.event_type)) return localizedFailure(t, payload.code);
    if (/degraded/.test(event.event_type)) return t("degraded");
    if (event.event_type === "ranking.reasons_progress") {
      return `${t(payload.phase === "reasons_retry" ? "reasonRetryGroups" : "reasonGroups")} ${Number(payload.completed_groups ?? 0)} / ${Number(payload.group_count ?? 0)} · ${Number(payload.paper_count ?? 0)} ${t("papersUnit")}`;
    }
    const current = Number(payload.completed_batches ?? 0);
    const total = Number(payload.batch_count ?? 0);
    const scored = Number(payload.scored_count ?? 0);
    const candidates = Number(payload.candidate_count ?? 0);
    return `${t("rankingBatches")} ${current} / ${total} · ${t("sentToLlm")} ${scored || candidates}`;
  }

  if (event.event_type === "workflow.completed") {
    return `${t("finalOutput")}: ${Number(payload.result_count ?? 0)} · ${t("warnings")}: ${Number(payload.warning_count ?? 0)}`;
  }
  if (event.event_type === "workflow.recovery") return t("recoveryApplied");
  if (event.event_type === "enrichment.completed") {
    return `${t("journalMatches")}: ${Number(payload.journal_matches ?? 0)} · ${t("acceptanceMatches")}: ${Number(payload.acceptance_matches ?? 0)}`;
  }
  return t("progressUpdated");
}

export function progressRows(t: TFunc, events: RunEvent[], prefix: string): StructuredRow[] {
  return events
    .filter((event) => event.event_type.startsWith(`${prefix}.`))
    .map((event) => ({ sequence: event.sequence, label: eventLabel(t, event.event_type), detail: eventDetail(t, event) }));
}

export function portfolioDetail(t: TFunc, item: Record<string, unknown>): string {
  const facts = [item.purpose, item.query].filter(Boolean).map(String);
  if (item.status) facts.push(t(String(item.status)));
  if (item.hit_count !== undefined) facts.push(`${t("retrievedPapers")} ${Number(item.hit_count)}`);
  if (item.added_unique !== undefined) facts.push(`${t("uniqueCandidates")} ${Number(item.added_unique)}`);
  return facts.join(" · ");
}

