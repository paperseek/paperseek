<script setup lang="ts">
import { computed } from "vue";
import Disclosure from "../../components/Disclosure.vue";
import Icon, { type IconName } from "../../components/Icon.vue";

import type { RunAudit, RunEvent, RunStep } from "../../api/types";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { attemptInfo, familyEvents, lastPayload, type ScopedEvents } from "./run-attempt";
import { boundaryLabel, retryLabel, retryScopeLabel, stageRetryStep, SUBSTEP_RETRY_STEP } from "./retry-steps";
import { localizedFailure, progressRows } from "./workflow-format";

type StepStatus = "pending" | "current" | "done" | "degraded" | "failed" | "skipped" | "reused" | "waiting";

interface StepRow {
  id: string;
  status: StepStatus;
  detail: string;
  progress: { current: number; total: number } | null;
  /** "Kept from the previous run" / "waiting to re-run", when the row is not fresh. */
  marker?: string;
  /** The boundary a retry control on this row re-runs, when the row is in trouble. */
  retry?: RunStep | null;
}

const props = defineProps<{
  events: RunEvent[];
  audit: RunAudit | null;
  /** The step currently being retried, so its button shows progress. */
  retrying?: RunStep | "";
  /** The ranking stage failed without a sub-step row that can represent it. */
  stageFailed?: boolean;
}>();

const emit = defineEmits<{ retry: [step: RunStep] }>();

const { t } = useLocale();
const u = useUi();

const attempt = computed(() => attemptInfo(props.events));
function family(prefix: string, boundary: RunStep): ScopedEvents {
  return familyEvents(props.events, prefix, boundary, attempt.value);
}

// One scope per pipeline boundary: a retry keeps the upstream checkpoints, so
// those rows show the previous attempt's numbers instead of stale "fresh" ones.
const retrieval = computed(() => family("retrieval.", "retrieval"));
const embedding = computed(() => family("retrieval.embedding_completed", "retrieval"));
const citation = computed(() => family("citation.", "citation"));
const selection = computed(() => family("selection.", "selection"));
const incremental = computed(() => family("selection.embedding_completed", "selection"));
const rerank = computed(() => family("selection.rerank_completed", "selection"));
const decision = computed(() => family("selection.decision_", "selection"));
const ranking = computed(() => family("ranking.", "ranking"));
const results = computed(() => family("workflow.", "results"));

/** Mark a derived row as kept from the previous attempt or as waiting to re-run. */
function scoped(scope: ScopedEvents, row: Omit<StepRow, "id" | "retry">): Omit<StepRow, "id" | "retry"> {
  if (scope.waiting) return { status: "waiting", detail: "", progress: null, marker: u("stepWaitingRerun") };
  if (scope.reused) return { ...row, status: "reused", progress: null, marker: u("stepReused") };
  return row;
}

// §5.2 number semantics: each metric renders only when its source field exists.
const metrics = computed(() => {
  const rows: Array<{ value: string; label: string }> = [];
  const retrievalCompleted = lastPayload(retrieval.value.events, "retrieval.completed");
  if (retrievalCompleted) {
    rows.push({ value: String(Number(retrievalCompleted.candidate_count ?? 0)), label: t("loadedCandidates") });
    rows.push({ value: String(Number(retrievalCompleted.total_candidates ?? 0)), label: t("fusedCandidates") });
  }
  const rankingPayload = lastPayload(ranking.value.events, "ranking.completed") || lastPayload(ranking.value.events, "ranking.started");
  if (rankingPayload && rankingPayload.candidate_count !== undefined) {
    rows.push({ value: String(Number(rankingPayload.candidate_count)), label: t("sentToLlm") });
  }
  // The final output is re-assembled by every retry, so it is never reused.
  const completed = lastPayload(results.value.events, "workflow.completed");
  if (completed && completed.result_count !== undefined) {
    rows.push({ value: String(Number(completed.result_count)), label: t("finalOutput") });
  }
  return rows;
});

const rawRankingSteps = computed<Array<Omit<StepRow, "retry">>>(() => {
  const rows: Array<Omit<StepRow, "retry">> = [];
  if (retrieval.value.all.length) {
    const events = retrieval.value.events;
    const progress = lastPayload(events, "retrieval.progress");
    const completed = lastPayload(events, "retrieval.completed");
    const failed = events.findLast((event) => event.event_type === "retrieval.failed")?.payload || null;
    const degraded = lastPayload(events, "retrieval.degraded");
    let status: StepStatus = "current";
    if (failed) status = "failed";
    else if (degraded) status = "degraded";
    else if (completed) status = "done";
    const numbers = completed
      ? `${t("fusedCandidates")} ${Number(completed.total_candidates ?? 0)} · ${t("loadedCandidates")} ${Number(completed.candidate_count ?? 0)}`
      : progress
        ? `${Number(progress.completed_lanes ?? 0)} / ${Number(progress.total_lanes ?? 0)} · ${Number(progress.candidate_count ?? 0)} ${t("candidates")}`
        : "";
    const detail = failed
      ? `${localizedFailure(t, failed.code)}${failed.failed_candidates !== undefined ? ` · ${t("failedCandidates")} ${Number(failed.failed_candidates)}` : ""}`
      : status === "degraded" && numbers
        ? `${t("degraded")} · ${numbers}`
        : numbers;
    rows.push({
      id: "stepMultiLane",
      ...scoped(retrieval.value, {
        status,
        detail,
        progress: progress && status === "current"
          ? { current: Number(progress.completed_lanes ?? 0), total: Number(progress.total_lanes ?? 0) }
          : null,
      }),
    });
  }
  if (embedding.value.all.length) {
    const payload = lastPayload(embedding.value.events, "retrieval.embedding_completed");
    rows.push({
      id: "stepEmbedding",
      ...scoped(embedding.value, {
        status: payload ? "done" : "current",
        detail: payload ? `${payload.model} · ${Number(payload.scored_count)} ${t("papersUnit")}` : "",
        progress: null,
      }),
    });
  }
  if (citation.value.all.length) {
    const events = citation.value.events;
    const latest = events[events.length - 1] || null;
    const payload = latest?.payload || {};
    let status: StepStatus = "current";
    let detail = "";
    if (latest?.event_type === "citation.completed") {
      status = "done";
      detail = `${Number(payload.added_candidates ?? 0)} / ${Number(payload.candidate_count ?? 0)} ${t("candidates")}`;
    } else if (latest?.event_type === "citation.skipped") {
      status = "skipped";
    } else if (latest?.event_type === "citation.degraded") {
      status = "degraded";
      detail = String(payload.code || t("degraded"));
    } else if (latest) {
      detail = `${Number(payload.processed_seeds ?? 0)} / ${Number(payload.seed_count ?? 0)}`;
    }
    rows.push({
      id: "stepCitation",
      ...scoped(citation.value, {
        status,
        detail,
        progress: latest && status === "current"
          ? { current: Number(payload.processed_seeds ?? 0), total: Number(payload.seed_count ?? 0) }
          : null,
      }),
    });
  }
  if (incremental.value.all.length) {
    const payload = lastPayload(incremental.value.events, "selection.embedding_completed");
    rows.push({
      id: "stepIncrementalEmbedding",
      ...scoped(incremental.value, {
        status: payload ? "done" : "current",
        detail: payload ? `${t("reusedScores")} ${Number(payload.reused_count)} · ${t("newScores")} ${Number(payload.scored_count)}` : "",
        progress: null,
      }),
    });
  }
  if (rerank.value.all.length) {
    const payload = lastPayload(rerank.value.events, "selection.rerank_completed");
    rows.push({
      id: "stepRerank",
      ...scoped(rerank.value, {
        status: payload ? "done" : "current",
        detail: payload ? `${payload.model} · ${Number(payload.scored_count)} ${t("papersUnit")}` : "",
        progress: null,
      }),
    });
  }
  if (decision.value.all.length) {
    const events = decision.value.events;
    const latest = lastPayload(events, "selection.decision_");
    const decisionFailed = lastPayload(events, "selection.decision_degraded");
    const decisionDone = lastPayload(events, "selection.decision_completed");
    if (latest) {
      rows.push({
        id: "stepDecision",
        ...scoped(decision.value, {
          status: decisionFailed ? "degraded" : decisionDone ? "done" : "current",
          detail: decisionFailed
            ? localizedFailure(t, decisionFailed.code)
            : `${latest.resolved_model || latest.model} · ${Number(latest.scored_count)} / ${Number(latest.candidate_count)} ${t("papersUnit")}`,
          progress: !decisionFailed && !decisionDone
            ? { current: Number(latest.scored_count), total: Number(latest.candidate_count) }
            : null,
        }),
      });
    }
  }
  if (selection.value.all.length) {
    const events = selection.value.events;
    const completed = lastPayload(events, "selection.completed");
    const started = lastPayload(events, "selection.started");
    const degraded = lastPayload(events, "selection.degraded") || lastPayload(events, "selection.decision_degraded");
    rows.push({
      id: "stepCandidateSelection",
      ...scoped(selection.value, {
        status: degraded ? "degraded" : completed ? "done" : "current",
        detail: completed
          ? completed.llm_gate_candidate_count !== undefined
            ? `${t("sentToLlm")} ${Number(completed.llm_gate_candidate_count)}`
            : `${Number(completed.candidate_count)} ${t("candidates")}`
          : started
            ? `${Number(started.candidate_count)} ${t("candidates")}`
            : "",
        progress: null,
      }),
    });
  }
  if (ranking.value.all.length) {
    const events = ranking.value.events;
    const completed = lastPayload(events, "ranking.completed");
    const failed = lastPayload(events, "ranking.failed");
    const degraded = lastPayload(events, "ranking.degraded");
    const batch = lastPayload(events, "ranking.batch_completed") || lastPayload(events, "ranking.started");
    let status: StepStatus = "current";
    if (failed) status = "failed";
    else if (degraded) status = "degraded";
    else if (completed) status = "done";
    const current = Number(batch?.completed_batches ?? 0);
    const total = Number(batch?.batch_count ?? 0);
    const numbers = completed ? `${Number(completed.scored_count ?? 0)} / ${Number(completed.candidate_count ?? 0)}` : "";
    const detail = failed
      ? `${localizedFailure(t, failed.code)}${failed.failed_candidates !== undefined ? ` · ${t("failedCandidates")} ${Number(failed.failed_candidates)}` : ""}`
      : status === "degraded" && numbers
        ? `${t("degraded")} · ${numbers}`
        : completed
          ? `${numbers}${Number(completed.warning_count ?? 0) ? ` · ${Number(completed.warning_count)} ${t("warnings")}` : ""}`
          : batch
            ? `${current} / ${total}`
            : "";
    rows.push({
      id: "stepLlmRanking",
      ...scoped(ranking.value, {
        status,
        detail,
        progress: status === "current" && total ? { current, total } : null,
      }),
    });
  }
  return rows;
});

const rankingSteps = computed<StepRow[]>(() => rawRankingSteps.value.map((row) => ({
  ...row,
  retry: row.status === "failed" || row.status === "degraded" ? SUBSTEP_RETRY_STEP[row.id] || null : null,
})));

function requestRetry(row: StepRow): void {
  if (row.retry) emit("retry", row.retry);
}

/**
 * A hard failure inside the ranking stage may have no sub-step row that can be
 * retried (there is no `ranking.failed` event kind), so the stage offers one
 * fallback control naming the boundary the backend would restart.
 */
const fallbackStep = computed<RunStep | null>(() => {
  if (!props.stageFailed) return null;
  if (rankingSteps.value.some((row) => row.retry)) return null;
  return stageRetryStep("ranking", attempt.value.events);
});

function retryFallback(): void {
  if (fallbackStep.value) emit("retry", fallbackStep.value);
}

function stepIcon(status: StepStatus): IconName {
  return ({ done: "ok", current: "loader", failed: "fail", degraded: "warn", skipped: "circle-minus", pending: "circle-minus", reused: "history", waiting: "clock" } as const)[status];
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
  { key: "retrieval", title: t("retrievalProgress"), testid: "retrieval-progress-audit", rows: progressRows(t, retrieval.value.events, "retrieval") },
  { key: "ranking", title: t("rankingProgress"), testid: "ranking-progress-audit", rows: progressRows(t, ranking.value.events, "ranking") },
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
          <Icon :name="stepIcon(step.status)" :class="{ muted: ['skipped', 'pending', 'reused', 'waiting'].includes(step.status) }" />
          <span class="grow sm" :class="{ medium: step.status === 'current' }">{{ t(step.id) }}</span>
          <span class="sm num" :class="{ muted: step.status !== 'current' }">{{ step.detail || stepStatusText(step.status) }}</span>
          <span v-if="step.marker" class="badge badge-outline muted" :data-testid="`${step.id}-marker`">{{ step.marker }}</span>
          <button
            v-if="step.retry"
            class="btn btn-ghost btn-sm"
            type="button"
            :title="retryScopeLabel(u, step.retry)"
            :disabled="Boolean(retrying)"
            :data-testid="`${step.id}-retry`"
            @click="requestRetry(step)"
          >
            <Icon :name="retrying === step.retry ? 'loader' : 'refresh'" size="sm" />
            {{ retrying === step.retry ? u("retryingBoundary", { step: boundaryLabel(u, step.retry) }) : retryLabel(u, step.retry) }}
          </button>
          <span v-if="step.progress && step.progress.total" class="progress thin" style="flex-basis: 100%; margin-left: 26px" role="progressbar" :aria-valuenow="step.progress.current" :aria-valuemax="step.progress.total"><span :style="{ width: `${Math.min(100, (step.progress.current / step.progress.total) * 100)}%` }"></span></span>
        </li>
      </ol>
      <button
        v-if="fallbackStep"
        class="btn btn-ghost btn-sm"
        type="button"
        style="align-self: flex-start"
        :title="retryScopeLabel(u, fallbackStep)"
        :disabled="Boolean(retrying)"
        data-testid="stage-ranking-retry"
        @click="retryFallback"
      >
        <Icon :name="retrying === fallbackStep ? 'loader' : 'refresh'" size="sm" />
        {{ retrying === fallbackStep ? u("retryingBoundary", { step: boundaryLabel(u, fallbackStep) }) : retryLabel(u, fallbackStep) }}
      </button>
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
