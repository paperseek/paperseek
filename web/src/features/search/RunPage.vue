<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch, watchEffect } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppDialog from "../../components/AppDialog.vue";
import Icon from "../../components/Icon.vue";
import QueueStatusCard from "../../components/QueueStatusCard.vue";
import { toast } from "../../components/toast";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { cancelRun, recoverRun, runState, stopRunRecovery } from "../../state/run";
import { loadSettings, workspaceSettings } from "../settings/store";
import { invalidateHistory, listPaths, setHeader } from "../workspace/state";
import RunSettingsDialog from "./RunSettingsDialog.vue";
import RunToolbar from "./RunToolbar.vue";
import { boundaryLabel } from "./retry-steps";
import { useSearchConfiguration } from "./use-search-configuration";
import WorkflowStages from "./WorkflowStages.vue";

const props = defineProps<{ externalRecovery?: boolean }>();
const route = useRoute();
const router = useRouter();
const { t, runMessage, sourceName } = useLocale();
const u = useUi();
const runId = computed(() => String(route.params.runId || ""));
const pageState = ref<"loading" | "error" | "loaded">("loading");
const confirmStop = ref(false);
const run = computed(() => (runState.run?.id === runId.value ? runState.run : null));
const status = computed(() => run.value?.status || "");
const terminal = computed(() => ["completed", "completed_with_warnings", "failed", "cancelled"].includes(status.value));
const active = computed(() => ["queued", "running", "cancelling"].includes(status.value));
const { restoreRun } = useSearchConfiguration(computed(() => workspaceSettings.descriptors));

// A retry requeues the run: keep the stages on screen and say which step is
// being re-run instead of swapping them for the fresh-submission queue card.
const retryNotice = computed(() => runState.retryNotice ? u("retryQueued", { step: boundaryLabel(u, runState.retryNotice) }) : "");

watchEffect(() => setHeader(run.value?.display_title || run.value?.question || u("search"), { label: u("search"), to: listPaths.search }));

const errorMessage = computed(() => {
  let code = run.value?.error_code || "";
  if (code === "research_workflow_failed") {
    const failure = [...runState.events].reverse().find((event) => event.event_type.endsWith(".failed") && event.event_type !== "run.failed");
    code = String(failure?.payload?.code || code);
  }
  const source = run.value?.source;
  return `${source && (code.startsWith("source_") || code === "query_primary_source_failed") ? `${sourceName(source)}: ` : ""}${runMessage(code, "error")}`;
});

async function load(value: string) {
  if (!value) return;
  pageState.value = "loading";
  try {
    await recoverRun(value);
    if (runId.value === value) pageState.value = "loaded";
  } catch (error) {
    if (runId.value === value && !(error instanceof Error && error.name === "AbortError")) {
      runState.error = error instanceof Error ? error.message : "run_unavailable";
      pageState.value = "error";
    }
  }
}
watch(runId, (id) => {
  if (id && !props.externalRecovery) void load(id);
}, { immediate: true });
onBeforeUnmount(() => {
  if (!props.externalRecovery) stopRunRecovery();
});
// Finishing while the page is open refreshes the sidebar and offers the results.
watch(status, (next, previous) => {
  if (!previous || next === previous) return;
  invalidateHistory("search");
  if (["completed", "completed_with_warnings"].includes(next) && run.value && document.visibilityState === "visible") {
    const id = run.value.id;
    toast(u("runDoneToast"), { tone: "ok", action: { label: u("viewResults"), run: () => void router.push(`/runs/${id}/results`) } });
  }
});

async function stop() {
  confirmStop.value = false;
  try {
    await cancelRun(runId.value);
    invalidateHistory("search");
  } catch (error) {
    if (!(error instanceof Error && error.name === "AbortError")) runState.error = error instanceof Error ? error.message : "cancel_unavailable";
  }
}
async function rerun() {
  await loadSettings();
  restoreRun(run.value);
  void router.push("/search");
}
</script>

<template>
  <main class="page">
    <RunToolbar :run="run" tab="run">
      <RunSettingsDialog :run="run" :can-rerun="terminal" @rerun="rerun" />
      <button v-if="status === 'running' || status === 'cancelling'" class="btn btn-outline btn-sm danger" type="button" :disabled="runState.cancelPending || status === 'cancelling'" @click="confirmStop = true">
        <Icon :name="runState.cancelPending || status === 'cancelling' ? 'loader' : 'square'" />{{ runState.cancelPending || status === "cancelling" ? u("stopping") : u("stop") }}
      </button>
      <button v-else-if="terminal" class="btn btn-outline btn-sm" type="button" data-testid="rerun-edit" @click="rerun">{{ u("rerun") }}</button>
    </RunToolbar>

    <template v-if="!run && pageState === 'loading'">
      <div class="card flush" data-testid="run-loading" aria-busy="true">
        <div v-for="index in 4" :key="index" class="stage" style="cursor: default"><span class="skeleton" style="width: 24px; height: 24px; border-radius: 999px"></span><span class="skeleton" style="width: 160px; height: 14px"></span><span class="skeleton grow" style="height: 14px"></span></div>
      </div>
    </template>
    <div v-else-if="!run && pageState === 'error'" class="alert" role="alert">
      <Icon name="fail" />
      <p class="alert-title">{{ u("loadError") }}</p>
      <p class="alert-desc">{{ t(runState.error || "run_unavailable") }}</p>
      <div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="load(runId)">{{ u("retry") }}</button></div>
    </div>
    <template v-else-if="run">
      <div v-if="run.error_code" class="alert" role="alert" data-testid="run-primary-summary">
        <Icon name="fail" />
        <p class="alert-title">{{ u("runFailed") }}</p>
        <p class="alert-desc">{{ errorMessage }}</p>
        <div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="rerun">{{ u("rerun") }}</button></div>
      </div>
      <div v-if="run.warnings?.length" class="alert" data-testid="run-warnings" style="grid-template-columns: 16px 1fr">
        <Icon name="warn" />
        <p class="alert-title">{{ u("st_completed_with_warnings") }}</p>
        <ul class="alert-desc stack" style="gap: 2px"><li v-for="warning in run.warnings" :key="warning">{{ runMessage(warning) }}</li></ul>
      </div>
      <p v-if="active && runState.connection !== 'connected'" class="xs muted row" style="gap: 6px" role="status" data-testid="connection-notice"><Icon name="loader" size="sm" />{{ runState.connection === "offline" ? u("offline") : u("reconnecting") }}</p>
      <div v-if="retryNotice" class="alert" role="status" data-testid="retry-notice" style="grid-template-columns: 16px 1fr">
        <Icon name="loader" />
        <p class="alert-title">{{ retryNotice }}</p>
      </div>
      <QueueStatusCard
        v-if="status === 'queued' && !retryNotice"
        :run="run"
        :busy="runState.cancelPending"
        @cancel="confirmStop = true"
        @update="(value) => { if (runState.run?.id === value.id && runState.run.status === 'queued' && !runState.cancelPending) runState.run = value; }"
      />
      <WorkflowStages v-else @rerun="rerun" />
      <p v-if="runState.error" class="xs danger-text" role="alert">{{ t(runState.error) }}</p>
    </template>

    <AppDialog
      v-if="confirmStop"
      :title="status === 'queued' ? u('cancelQueueTitle') : u('stopConfirmTitle')"
      :description="status === 'queued' ? u('cancelQueueDesc') : u('stopConfirmDesc')"
      @close="confirmStop = false"
    >
      <template #footer>
        <button class="btn btn-outline btn-sm" type="button" @click="confirmStop = false">{{ u("keepRunning") }}</button>
        <button class="btn btn-destructive btn-sm" type="button" @click="stop">{{ status === "queued" ? u("cancelQueue") : u("stop") }}</button>
      </template>
    </AppDialog>
  </main>
</template>
