<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch, watchEffect } from "vue";
import Icon from "../../components/Icon.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { runState, type StageKey } from "../../state/run";
import StageQueryBody from "./StageQueryBody.vue";
import StageRankingBody from "./StageRankingBody.vue";
import StageResultsBody from "./StageResultsBody.vue";
import StageSearchBody from "./StageSearchBody.vue";
import { eventStage, localizedDecision, localizedRefinementStop } from "./workflow-format";

type Disclosure = "auto" | "manually_open" | "manually_closed";
const { t } = useLocale();
const u = useUi();
const emit = defineEmits<{ rerun: [] }>();
const stages: StageKey[] = ["query", "search", "ranking", "results"];

// Three-state disclosure per stage within the current run.
const disclosure = reactive<Record<StageKey, Disclosure>>({ query: "auto", search: "auto", ranking: "auto", results: "auto" });
watch(() => runState.run?.id, () => {
  for (const key of stages) disclosure[key] = "auto";
});
for (const key of stages) {
  watch(() => runState.stages[key], (next, previous) => {
    if (next === "failed" && previous !== "failed") disclosure[key] = "auto";
  });
}
const terminal = computed(() => ["completed", "completed_with_warnings", "failed", "cancelled"].includes(runState.run?.status || ""));
const complete = computed(() => ["completed", "completed_with_warnings"].includes(runState.run?.status || ""));
const cancelled = computed(() => runState.run?.status === "cancelled");
function isOpen(key: StageKey): boolean {
  if (disclosure[key] === "manually_open") return true;
  if (disclosure[key] === "manually_closed") return false;
  if (runState.stages[key] === "failed") return true;
  return runState.stages[key] === "active" && !terminal.value;
}
function toggle(key: StageKey) {
  disclosure[key] = isOpen(key) ? "manually_closed" : "manually_open";
}

const now = ref(Date.now());
// The clock only ticks while a stage is running; a finished run shows fixed durations.
const ticking = computed(() => !terminal.value && stages.some((key) => runState.stages[key] === "active"));
let timer: ReturnType<typeof setInterval> | undefined;
watch(ticking, (value) => {
  clearInterval(timer);
  timer = undefined;
  if (!value) return;
  now.value = Date.now();
  timer = setInterval(() => (now.value = Date.now()), 1000);
}, { immediate: true });
onBeforeUnmount(() => clearInterval(timer));
function clock(seconds: number) {
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function elapsed(key: StageKey): string {
  const started = Date.parse(runState.stageStartedAt[key] || "");
  if (Number.isNaN(started)) return "";
  if (runState.stages[key] === "active" && !terminal.value) return clock(Math.max(0, Math.floor((now.value - started) / 1000)));
  if (runState.stages[key] !== "complete") return "";
  const next = stages.slice(stages.indexOf(key) + 1).map((stage) => Date.parse(runState.stageStartedAt[stage] || "")).find((value) => !Number.isNaN(value));
  return next ? clock(Math.max(0, Math.floor((next - started) / 1000))) : "";
}

const stageEvents = computed(() => {
  const lists: Record<StageKey, typeof runState.events> = { query: [], search: [], ranking: [], results: [] };
  for (const event of runState.events) {
    const key = eventStage(event);
    if (key) lists[key].push(event);
  }
  return lists;
});
function lastPayload(prefix: string): Record<string, unknown> | null {
  for (let index = runState.events.length - 1; index >= 0; index -= 1) {
    if (runState.events[index].event_type.startsWith(prefix)) return runState.events[index].payload || {};
  }
  return null;
}
const resultCount = computed(() => runState.run?.result_count ?? Number(lastPayload("workflow.completed")?.result_count ?? NaN));
function summary(key: StageKey): string {
  if (key === "query") {
    const planned = lastPayload("query.planned");
    return planned ? String(planned.intent || planned.current_query || "") : "";
  }
  if (key === "search") {
    const stopped = lastPayload("query.refinement_stopped");
    if (stopped) return localizedRefinementStop(t, stopped.code);
    const selected = lastPayload("query.selected");
    if (selected) return t("querySelectionReason").replace("{n}", String(selected.selected_iteration));
    const latest = lastPayload("query.progress");
    if (!latest) return "";
    return [`${Number(latest.iteration) || 0} ${t("iterations")}`, localizedDecision(t, latest.decision), `${t("retrievedPapers")} ${Number(latest.hit_count ?? 0)}`].join(" · ");
  }
  if (key === "ranking") {
    const parts: string[] = [];
    const retrieval = lastPayload("retrieval.completed");
    if (retrieval) parts.push(`${t("fusedCandidates")} ${Number(retrieval.total_candidates ?? 0)}`);
    const ranking = lastPayload("ranking.completed") || lastPayload("ranking.");
    if (ranking && ranking.scored_count !== undefined) parts.push(`${t("sentToLlm")} ${Number(ranking.scored_count)}`);
    return parts.join(" · ");
  }
  return Number.isFinite(resultCount.value) && complete.value ? u("resultsReady", { n: resultCount.value }) : "";
}
function hint(key: StageKey) {
  return u(`stageHint_${key}` as "stageHint_query");
}

const announcement = ref("");
let lastStage = "";
let lastStatus = "";
watchEffect(() => {
  const status = runState.run?.status || "";
  if (status && status !== lastStatus && terminal.value) {
    announcement.value = u(`st_${status}` as "st_completed");
    lastStatus = status;
    return;
  }
  const active = stages.find((key) => runState.stages[key] === "active");
  if (active && active !== lastStage) {
    announcement.value = `${t("announceStage")}: ${u(`stage_${active}` as "stage_query")}`;
    lastStage = active;
  }
});
</script>

<template>
  <section class="card flush" :aria-label="u('tabRun')">
    <p class="sr-only" aria-live="polite">{{ announcement }}</p>
    <details v-for="(key, index) in stages" :key="key" :open="isOpen(key)" :data-testid="`stage-${key}`" :data-state="runState.stages[key]">
      <summary
        class="stage"
        :data-testid="`stage-${key}-toggle`"
        :style="runState.stages[key] === 'active' && !terminal ? 'background: rgb(245 245 245 / .6)' : undefined"
        :aria-expanded="isOpen(key)"
        @click.prevent="toggle(key)"
      >
        <span v-if="runState.stages[key] === 'complete'" class="step-dot done"><Icon name="check" size="sm" /></span>
        <span v-else-if="runState.stages[key] === 'failed'" class="step-dot" style="border-color: var(--destructive); color: var(--destructive)"><Icon name="x" size="sm" /></span>
        <span v-else class="step-dot" :class="{ active: runState.stages[key] === 'active' && !terminal }">{{ index + 1 }}</span>
        <span class="sm medium" :class="{ muted: runState.stages[key] === 'idle' }" style="width: 160px; flex-shrink: 0">{{ u(`stage_${key}` as "stage_query") }}</span>
        <span class="sm muted truncate grow">{{ summary(key) || (terminal && runState.stages[key] === "idle" ? (cancelled ? u("st_cancelled") : u("notRun")) : hint(key)) }}</span>
        <span v-if="runState.stages[key] === 'active' && !terminal" class="badge badge-outline muted"><Icon name="loader" />{{ u("inProgress") }}</span>
        <span v-else-if="runState.stages[key] === 'failed'" class="badge badge-outline muted"><Icon name="fail" />{{ u("st_failed") }}</span>
        <span v-if="elapsed(key)" class="xs muted num" :data-testid="`stage-${key}-elapsed`">{{ elapsed(key) }}</span>
        <RouterLink v-if="key === 'results' && complete && runState.run" class="btn btn-sm" style="height: 28px" :to="`/runs/${runState.run.id}/results`" @click.stop>{{ u("viewResults") }}</RouterLink>
        <Icon name="chevron-down" class="muted" :style="{ transform: isOpen(key) ? 'rotate(180deg)' : undefined }" />
      </summary>
      <div class="stack" style="gap: 12px; padding: 4px 16px 16px 52px">
        <StageQueryBody v-if="key === 'query'" :events="stageEvents.query" :source="runState.run?.source || ''" />
        <StageSearchBody v-else-if="key === 'search'" :events="stageEvents.search" :audit="runState.audit" :stage-active="runState.stages.search === 'active' && !terminal" />
        <StageRankingBody v-else-if="key === 'ranking'" :events="runState.events" :audit="runState.audit" />
        <StageResultsBody v-else :run="runState.run" :events="stageEvents.results" />
        <p v-if="cancelled && runState.stages[key] !== 'complete'" class="help">{{ t("stageCancelledNote") }}</p>
        <div v-if="runState.stages[key] === 'failed'" class="row" style="margin-left: -12px">
          <button class="btn btn-ghost btn-sm" type="button" @click="emit('rerun')">{{ u("rerun") }}</button>
        </div>
      </div>
    </details>
  </section>
</template>
