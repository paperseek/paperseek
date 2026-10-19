<script setup lang="ts">
import { computed } from "vue";
import Disclosure from "../../components/Disclosure.vue";
import Icon, { type IconName } from "../../components/Icon.vue";

import type { RunAudit, RunEvent } from "../../api/types";
import { useLocale } from "../../locales";
import { localizedFailure, progressRows } from "./workflow-format";

type StepStatus = "pending" | "current" | "done" | "degraded" | "failed" | "skipped";

interface StepRow {
  id: string;
  status: StepStatus;
  detail: string;
  progress: { current: number; total: number } | null;
}

const props = defineProps<{
  events: RunEvent[];
  audit: RunAudit | null;
}>();

const { t } = useLocale();

function lastPayload(prefix: string): Record<string, unknown> | null {
  for (let index = props.events.length - 1; index >= 0; index -= 1) {
    const event = props.events[index];
    if (event.event_type.startsWith(prefix)) return event.payload || {};
  }
  return null;
}

const retrievalCompleted = computed(() => lastPayload("retrieval.completed"));
const workflowCompleted = computed(() => lastPayload("workflow.completed"));

// §5.2 number semantics: each metric renders only when its source field exists.
const metrics = computed(() => {
  const rows: Array<{ value: string; label: string }> = [];
  if (retrievalCompleted.value) {
    rows.push({ value: String(Number(retrievalCompleted.value.candidate_count ?? 0)), label: t("loadedCandidates") });
    rows.push({ value: String(Number(retrievalCompleted.value.total_candidates ?? 0)), label: t("fusedCandidates") });
  }
  const ranking = lastPayload("ranking.completed") || lastPayload("ranking.started");
  if (ranking && ranking.candidate_count !== undefined) {
    rows.push({ value: String(Number(ranking.candidate_count)), label: t("sentToLlm") });
  }
  if (workflowCompleted.value && workflowCompleted.value.result_count !== undefined) {
    rows.push({ value: String(Number(workflowCompleted.value.result_count)), label: t("finalOutput") });
  }
  return rows;
});

const rankingSteps = computed<StepRow[]>(() => {
  const rows: StepRow[] = [];
  const retrievalEvents = props.events.filter((event) => event.event_type.startsWith("retrieval."));
  if (retrievalEvents.length) {
    const progress = lastPayload("retrieval.progress");
    const completed = retrievalCompleted.value;
    const failed = retrievalEvents.some((event) => event.event_type === "retrieval.failed");
    const degradedPayload = lastPayload("retrieval.degraded");
    let status: StepStatus = "current";
    if (failed) status = "failed";
    else if (completed) status = "done";
    else if (degradedPayload && !completed) status = "degraded";
    const detail = completed
      ? `${t("fusedCandidates")} ${Number(completed.total_candidates ?? 0)} · ${t("loadedCandidates")} ${Number(completed.candidate_count ?? 0)}`
      : progress
        ? `${Number(progress.completed_lanes ?? 0)} / ${Number(progress.total_lanes ?? 0)} · ${Number(progress.candidate_count ?? 0)} ${t("candidates")}`
        : "";
    rows.push({
      id: "stepMultiLane",
      status,
      detail,
      progress: progress && !completed
        ? { current: Number(progress.completed_lanes ?? 0), total: Number(progress.total_lanes ?? 0) }
        : null,
    });
  }
  const embedding = lastPayload("retrieval.embedding_completed");
  if (embedding) rows.push({
    id: "stepEmbedding", status: "done",
    detail: `${embedding.model} · ${Number(embedding.scored_count)} ${t("papersUnit")}`,
    progress: null,
  });
  const citationEvents = props.events.filter((event) => event.event_type.startsWith("citation."));
  if (citationEvents.length) {
    const latestEvent = citationEvents[citationEvents.length - 1];
    const payload = latestEvent.payload || {};
    let status: StepStatus = "current";
    let detail = "";
    if (latestEvent.event_type === "citation.completed") {
      status = "done";
      detail = `${Number(payload.added_candidates ?? 0)} / ${Number(payload.candidate_count ?? 0)} ${t("candidates")}`;
    } else if (latestEvent.event_type === "citation.skipped") {
      status = "skipped";
    } else if (latestEvent.event_type === "citation.degraded") {
      status = "degraded";
      detail = String(payload.code || t("degraded"));
    } else {
      detail = `${Number(payload.processed_seeds ?? 0)} / ${Number(payload.seed_count ?? 0)}`;
    }
    rows.push({
      id: "stepCitation",
      status,
      detail,
      progress: status === "current"
        ? { current: Number(payload.processed_seeds ?? 0), total: Number(payload.seed_count ?? 0) }
        : null,
    });
  }
  const incremental = lastPayload("selection.embedding_completed");
  if (incremental) rows.push({
    id: "stepIncrementalEmbedding", status: "done",
    detail: `${t("reusedScores")} ${Number(incremental.reused_count)} · ${t("newScores")} ${Number(incremental.scored_count)}`,
    progress: null,
  });
  const rerank = lastPayload("selection.rerank_completed");
  if (rerank) rows.push({
    id: "stepRerank", status: "done",
    detail: `${rerank.model} · ${Number(rerank.scored_count)} ${t("papersUnit")}`,
    progress: null,
  });
  const decision = lastPayload("selection.decision_");
  const decisionFailed = lastPayload("selection.decision_degraded");
  const decisionDone = lastPayload("selection.decision_completed");
  if (decision) rows.push({
    id: "stepDecision", status: decisionFailed ? "degraded" : decisionDone ? "done" : "current",
    detail: decisionFailed ? localizedFailure(t, decisionFailed.code)
      : `${decision.resolved_model || decision.model} · ${Number(decision.scored_count)} / ${Number(decision.candidate_count)} ${t("papersUnit")}`,
    progress: !decisionFailed && !decisionDone ? { current: Number(decision.scored_count), total: Number(decision.candidate_count) } : null,
  });
  const selection = lastPayload("selection.completed") || lastPayload("selection.started");
  if (selection) rows.push({
    id: "stepCandidateSelection",
    status: lastPayload("selection.degraded") || decisionFailed ? "degraded" : selection.llm_gate_candidate_count !== undefined ? "done" : "current",
    detail: selection.llm_gate_candidate_count !== undefined
      ? `${t("sentToLlm")} ${Number(selection.llm_gate_candidate_count)}`
      : `${Number(selection.candidate_count)} ${t("candidates")}`,
    progress: null,
  });
  const rankingEvents = props.events.filter((event) => event.event_type.startsWith("ranking."));
  if (rankingEvents.length) {
    const completedPayload = lastPayload("ranking.completed");
    const failedPayload = lastPayload("ranking.failed");
    const batch = lastPayload("ranking.batch_completed") || lastPayload("ranking.started");
    const degradedPayload = lastPayload("ranking.degraded");
    let status: StepStatus = "current";
    if (failedPayload) status = "failed";
    else if (completedPayload) status = "done";
    else if (degradedPayload) status = "degraded";
    // Scoring runs in batches; reasons are then written in groups. Each gets its own row.
    const reasons = lastPayload("ranking.reasons_progress");
    const scoringDone = Boolean(reasons || completedPayload);
    const current = Number(batch?.completed_batches ?? 0);
    const total = Number(batch?.batch_count ?? 0);
    const scoringStatus: StepStatus = failedPayload ? "failed" : scoringDone ? "done" : status;
    const detail = failedPayload
      ? `${localizedFailure(t, failedPayload.code)}${failedPayload.failed_candidates !== undefined ? ` · ${t("failedCandidates")} ${Number(failedPayload.failed_candidates)}` : ""}`
      : completedPayload
      ? `${Number(completedPayload.scored_count ?? 0)} / ${Number(completedPayload.candidate_count ?? 0)}${Number(completedPayload.warning_count ?? 0) ? ` · ${Number(completedPayload.warning_count)} ${t("warnings")}` : ""}`
      : batch
        ? `${t("rankingBatches")} ${current} / ${total}`
        : "";
    rows.push({
      id: "stepLlmRanking",
      status: scoringStatus,
      detail,
      progress: scoringStatus === "current" && total ? { current, total } : null,
    });
    if (reasons) {
      const groups = Number(reasons.group_count ?? 0);
      const written = Number(reasons.completed_groups ?? 0);
      const reasonStatus: StepStatus = completedPayload ? (degradedPayload?.code === "ranking_reasons_missing" ? "degraded" : "done") : "current";
      rows.push({
        id: "stepRankingReasons",
        status: reasonStatus,
        detail: completedPayload
          ? `${Number(reasons.paper_count ?? 0)} ${t("papersUnit")}`
          : `${t(reasons.phase === "reasons_retry" ? "reasonRetryGroups" : "reasonGroups")} ${written} / ${groups}`,
        progress: reasonStatus === "current" && groups ? { current: written, total: groups } : null,
      });
    }
  }
  return rows;
});

function stepIcon(status: StepStatus): IconName {
  return ({ done: "ok", current: "loader", failed: "fail", degraded: "warn", skipped: "circle-minus", pending: "circle-minus" } as const)[status];
}

function stepStatusText(status: StepStatus): string {
  if (status === "skipped") return t("not_requested");
  if (status === "done") return t("complete");
  if (status === "current") return t("active");
  if (status === "failed") return t("failed");
  if (status === "degraded") return t("degraded");
  return t("idle");
}

const processGroups = computed(() => [
  { key: "retrieval", title: t("retrievalProgress"), testid: "retrieval-progress-audit", rows: progressRows(t, props.events, "retrieval") },
  { key: "ranking", title: t("rankingProgress"), testid: "ranking-progress-audit", rows: progressRows(t, props.events, "ranking") },
].filter((group) => group.rows.length));

const recoveryJson = computed(() => JSON.stringify({
  candidate_lifecycle: props.audit?.candidate_lifecycle,
  recovery: props.audit?.recovery,
}, null, 2));
</script>

<template>
  <div class="stack" style="gap: 12px" data-testid="stage-ranking-audit">
    <p v-if="!events.length" class="sm muted">{{ t("waitingFirstResponse") }}</p>
    <template v-else>
      <div v-if="metrics.length" class="metrics">
        <div v-for="metric in metrics" :key="metric.label" class="metric"><span class="medium num">{{ Number(metric.value).toLocaleString() }}</span><span class="xs muted">{{ metric.label }}</span></div>
      </div>
      <ol v-if="rankingSteps.length" class="list">
        <li v-for="step in rankingSteps" :key="step.id" class="list-row" style="padding-left: 0; flex-wrap: wrap">
          <Icon :name="stepIcon(step.status)" :class="{ muted: step.status === 'skipped' || step.status === 'pending' }" />
          <span class="grow sm" :class="{ medium: step.status === 'current' }">{{ t(step.id) }}</span>
          <span class="sm num" :class="{ muted: step.status !== 'current' }">{{ step.detail || stepStatusText(step.status) }}</span>
          <span v-if="step.progress && step.progress.total" class="progress thin" style="flex-basis: 100%; margin-left: 26px" role="progressbar" :aria-valuenow="step.progress.current" :aria-valuemax="step.progress.total"><span :style="{ width: `${Math.min(100, (step.progress.current / step.progress.total) * 100)}%` }"></span></span>
        </li>
      </ol>
      <Disclosure
        v-if="processGroups.length || audit"
        :items="[
          ...(processGroups.length ? [{ key: 'process', label: t('processDetail'), testid: 'stage-ranking-process-toggle' }] : []),
          ...(audit ? [{ key: 'recovery', label: t('candidateRecovery'), testid: 'stage-ranking-audit-toggle' }] : []),
        ]"
      >
        <template #process>
          <section v-for="group in processGroups" :key="group.key" class="stack" style="gap: 4px">
            <h4 class="xs muted medium">{{ group.title }}</h4>
            <ol class="stack" style="gap: 4px" :data-testid="group.testid">
              <li v-for="row in group.rows" :key="row.sequence" class="sm"><span class="medium">{{ row.label }}</span><span v-if="row.detail" class="muted"> · {{ row.detail }}</span></li>
            </ol>
          </section>
        </template>
        <template #recovery><pre class="code" data-testid="candidate-recovery-audit">{{ recoveryJson }}</pre></template>
      </Disclosure>
    </template>
  </div>
</template>
