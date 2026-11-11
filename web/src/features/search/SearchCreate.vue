<script setup lang="ts">
import { computed, onMounted, ref, watch, watchEffect } from "vue";
import { useRouter } from "vue-router";
import { api, ApiError } from "../../api/client";
import type { HistoryItem, SourceDescriptor } from "../../api/types";
import Icon from "../../components/Icon.vue";
import UiSelect, { type SelectOption } from "../../components/UiSelect.vue";
import { useLocale } from "../../locales";
import { useUi, useUiError } from "../../locales/ui";
import { runState, submitSearch } from "../../state/run";
import { connectionById, connectionStatus, connectionsOf, loadSettings, sourceSetting, workspaceSettings } from "../settings/store";
import { invalidateHistory, listPaths, setHeader } from "../workspace/state";
import FieldSelector from "./FieldSelector.vue";
import { applyDefaults, type PreRankingMode } from "./search-draft";
import { useSearchConfiguration } from "./use-search-configuration";

// singleRun: the deployment accepts one active search at a time, so an active one blocks the form.
const props = withDefaults(defineProps<{ submissionEnabled?: boolean; singleRun?: boolean }>(), { submissionEnabled: true });
const router = useRouter();
const { t, sourceName } = useLocale();
const u = useUi();
const uiError = useUiError();
const sources = computed<SourceDescriptor[]>(() => workspaceSettings.descriptors);
const { form, configurationPayload } = useSearchConfiguration(sources);
const busy = ref(false);
const submitError = ref("");
const activeRun = ref<HistoryItem | null>(null);
const stopBusy = ref(false);
const moreOpen = ref(false);
const MAX = 10_000;

watchEffect(() => setHeader(u("newSearch"), { label: u("search"), to: listPaths.search }));
onMounted(async () => {
  await loadSettings(true);
  if (workspaceSettings.defaults) applyDefaults(workspaceSettings.defaults);
  if (!connectionById(form.llmConnection)) form.llmConnection = workspaceSettings.defaults?.search_llm || "";
  if (props.singleRun) void findActiveRun();
});
async function findActiveRun() {
  const run = runState.run;
  if (run && ["queued", "running", "cancelling"].includes(run.status)) {
    activeRun.value = { ...run, result_count: run.result_count ?? 0 } as unknown as HistoryItem;
    return;
  }
  try {
    const page = await api.history({ pageSize: 10 });
    activeRun.value = (page.items || []).find((item) => ["queued", "running", "cancelling"].includes(item.status)) || null;
  } catch {
    activeRun.value = null;
  }
}

const llms = computed(() => connectionsOf("llm"));
const usableSources = computed(() => sources.value.filter((source) => sourceSetting(source.id)?.usable));
const freeSources = computed(() => workspaceSettings.sources.filter((source) => {
  const descriptor = sources.value.find((item) => item.id === source.id);
  return descriptor?.api_key === "not_required" || descriptor?.api_key === "optional" || descriptor?.api_key === "recommended";
}).length);
const needsSetup = computed(() => workspaceSettings.loaded && !llms.value.length);
const selectedSource = computed(() => sources.value.find((source) => source.id === form.source));
const filter = computed(() => selectedSource.value?.capabilities.filter);
const filterLabel = computed(() => (filter.value?.mode === "native" ? u("fieldFilter") : u("fieldFilter")));
const supportsCitation = computed(() => Boolean(selectedSource.value?.capabilities.citation_expansion));

watch(() => form.source, (value, previous) => {
  if (previous && value !== previous) {
    form.fieldIds = [];
    form.fieldHint = "";
    form.citationEnabled = true;
  }
});
// Saved connections can change elsewhere (another tab, a deployment's service switch); keep a valid choice.
watch(() => workspaceSettings.connections, () => {
  if (workspaceSettings.loaded && !connectionById(form.llmConnection)) form.llmConnection = workspaceSettings.defaults?.search_llm || "";
});
watch(usableSources, (value) => {
  if (value.length && !value.some((source) => source.id === form.source))
    form.source = workspaceSettings.defaults?.source || value[0].id;
});

const sourceOptions = computed<SelectOption<string>[]>(() =>
  usableSources.value.map((source) => ({ value: source.id, label: sourceName(source.id, source.label) })),
);
const budgetOptions = computed<SelectOption<string>[]>(() =>
  ["auto", "fast", "medium", "high", "max"].map((value) => ({
    value,
    label: value === "auto" ? u("budget_auto") : t(value),
    hint: value === "auto" ? u("recommended") : undefined,
    desc: t(`budgetHint${value[0].toUpperCase()}${value.slice(1)}`),
  })),
);
const llmOptions = computed<SelectOption<string>[]>(() =>
  llms.value.map((item) => ({ value: item.id, label: item.model, desc: item.base_url.replace(/^https?:\/\//, ""), status: connectionStatus(item) })),
);
const preOptions = computed<SelectOption<PreRankingMode>[]>(() => {
  const missing = (kind: "embedding" | "decision") => !connectionsOf(kind).length;
  return (["local_fusion", "embedding_rerank", "decision"] as const).map((mode) => ({
    value: mode,
    label: u(`pre_${mode}`),
    desc: mode === "local_fusion" ? t("preRanking_local_fusion_help") : missing(mode === "decision" ? "decision" : "embedding") ? u("needsConnection") : t(`preRanking_${mode}_help`),
    disabled: mode !== "local_fusion" && missing(mode === "decision" ? "decision" : "embedding"),
  }));
});
const someUnconfigured = computed(() => preOptions.value.some((option) => option.disabled));

const changed = computed(() => {
  const defaults = workspaceSettings.defaults;
  if (!defaults) return 0;
  return [
    form.fieldIds.length > 0 || form.fieldHint.trim() !== "",
    form.llmConnection !== defaults.search_llm,
    form.preRankingMode !== defaults.pre_ranking,
    supportsCitation.value && !form.citationEnabled,
  ].filter(Boolean).length;
});

const blocker = computed(() => {
  if (needsSetup.value) return u("needLlmToStart");
  if (!form.question.trim()) return u("enterQuestion");
  if (activeRun.value) return u("activeRunTitle");
  return "";
});
const canSubmit = computed(() => props.submissionEnabled && !busy.value && !blocker.value && Boolean(form.source && form.llmConnection));

function reset() {
  if (workspaceSettings.defaults) applyDefaults(workspaceSettings.defaults, true);
  submitError.value = "";
}
async function submit() {
  if (!canSubmit.value) return;
  busy.value = true;
  submitError.value = "";
  try {
    const run = await submitSearch({ question: form.question, ...configurationPayload() });
    invalidateHistory("search");
    form.question = "";
    await router.push(`/runs/${run.id}`);
  } catch (error) {
    if (error instanceof ApiError && error.code === "active_run_exists") {
      await findActiveRun();
      if (!activeRun.value) submitError.value = "activeRunExists";
    } else {
      submitError.value = error instanceof ApiError ? error.code : "run_unavailable";
    }
  } finally {
    busy.value = false;
  }
}
async function stopActive() {
  if (!activeRun.value || stopBusy.value) return;
  stopBusy.value = true;
  try {
    await api.cancel(activeRun.value.id);
    activeRun.value = null;
    invalidateHistory("search");
  } catch (error) {
    submitError.value = error instanceof ApiError ? error.code : "cancel_unavailable";
  } finally {
    stopBusy.value = false;
  }
}
function shortcut(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    void submit();
  }
}
function errorText(code: string) {
  const value = t(code);
  return value !== code ? value : uiError(code);
}
</script>

<template>
  <main class="page create">
    <slot name="notice" />
    <!-- A deployment may supply its own first-use guide. -->
    <slot v-if="needsSetup" name="setup" :free-sources="freeSources">
      <section class="alert" style="grid-template-columns: 16px 1fr" aria-labelledby="setup-title">
        <Icon name="info" />
        <p id="setup-title" class="alert-title">{{ u("setupTitle") }}</p>
        <div class="alert-desc stack" style="margin-top: 4px">
          <div class="task"><span class="step-dot active" style="width: 20px; height: 20px; font-size: 11px">1</span><span class="sm" style="color: var(--foreground)">{{ u("setupLlm") }} <span class="muted">· {{ u("setupRequired") }}</span></span><RouterLink class="btn btn-sm" :to="{ path: '/settings/models', query: { add: 'llm' } }" style="height: 28px">{{ u("addConnection") }}</RouterLink></div>
          <div class="task"><span class="step-dot" style="width: 20px; height: 20px; font-size: 11px">2</span><span class="sm">{{ u("setupSources") }} <span class="muted">· {{ u("setupSourcesHint", { n: freeSources }) }}</span></span><RouterLink class="btn btn-ghost btn-sm" to="/settings/sources" style="height: 28px">{{ u("configure") }}</RouterLink></div>
          <div class="task"><span class="step-dot" style="width: 20px; height: 20px; font-size: 11px">3</span><span class="sm">{{ u("setupOptional") }}</span><RouterLink class="btn btn-ghost btn-sm" to="/settings/models" style="height: 28px">{{ u("configure") }}</RouterLink></div>
        </div>
      </section>
    </slot>
    <section v-else-if="activeRun" class="alert" role="status">
      <Icon name="loader" />
      <p class="alert-title">{{ u("activeRunTitle") }}</p>
      <p class="alert-desc truncate">{{ activeRun.display_title || activeRun.question }}</p>
      <div class="alert-act">
        <RouterLink class="btn btn-ghost btn-sm" :to="`/runs/${activeRun.id}`" style="height: 28px">{{ u("view") }}</RouterLink>
        <button class="btn btn-outline btn-sm danger" type="button" style="height: 28px" :disabled="stopBusy" @click="stopActive">{{ stopBusy ? u("stopping") : u("stop") }}</button>
      </div>
    </section>

    <form class="card create-card" autocomplete="off" aria-labelledby="new-search-title" novalidate @submit.prevent="submit" @keydown="shortcut">
      <div class="ch">
        <div><h2 id="new-search-title">{{ u("newSearch") }}</h2><p>{{ u("newSearchDesc") }}</p></div>
      </div>
      <div class="cc">
        <div class="field">
          <label class="label" for="research-question">{{ u("researchQuestion") }}</label>
          <textarea id="research-question" v-model="form.question" class="input textarea" rows="5" :maxlength="MAX" :placeholder="u('questionPlaceholder')" style="min-height: 120px; line-height: 22px"></textarea>
          <p class="help">{{ u("questionHelp") }}<span v-if="form.question.length > MAX * 0.8" class="num"> · {{ form.question.length.toLocaleString() }} / {{ MAX.toLocaleString() }}</span></p>
        </div>
        <div style="display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))">
          <div class="field">
            <span id="source-label" class="label">{{ u("dataSource") }}</span>
            <UiSelect v-model="form.source" :options="sourceOptions" :label="u('dataSource')" :placeholder="workspaceSettings.loading ? u('loading') : '—'" />
          </div>
          <div class="field">
            <span class="label">{{ u("searchBudget") }}</span>
            <UiSelect v-model="form.searchBudget" :options="budgetOptions" :label="u('searchBudget')" />
          </div>
        </div>
        <slot name="service" :source="form.source" />
        <details :open="moreOpen" style="border: 1px solid var(--border); border-radius: 10px" @toggle="moreOpen = ($event.target as HTMLDetailsElement).open">
          <summary class="row" style="cursor: pointer; padding: 10px 14px; justify-content: space-between; flex-wrap: nowrap">
            <span class="sm medium">{{ u("moreOptions") }}</span>
            <span class="row xs muted" style="gap: 6px; flex-wrap: nowrap"><template v-if="changed">{{ u("changedCount", { n: changed }) }}</template><Icon name="chevron-down" :style="{ transform: moreOpen ? 'rotate(180deg)' : undefined }" /></span>
          </summary>
          <div class="stack" style="gap: 16px; padding: 4px 14px 14px">
            <div style="display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr))">
              <div v-if="filter?.mode === 'native'" class="field">
                <span class="label">{{ filterLabel }}</span>
                <FieldSelector v-model="form.fieldIds" :source-id="form.source" :label="u('fieldDialogTitle')" :options="filter.options" />
              </div>
              <div v-else-if="filter?.mode === 'text'" class="field" data-testid="source-filter">
                <label class="label" for="field-hint">{{ filterLabel }}</label>
                <input id="field-hint" v-model="form.fieldHint" class="input" maxlength="500" :placeholder="u('fieldHintPh')" />
              </div>
              <div class="field">
                <span class="label">{{ u("searchModel") }}</span>
                <UiSelect v-model="form.llmConnection" :options="llmOptions" :label="u('searchModel')" :placeholder="u('needsConnection')">
                  <template #footer><div class="opt-sep"></div><RouterLink class="opt" to="/settings/models"><Icon name="plus" />{{ u("addConnection") }}</RouterLink></template>
                </UiSelect>
              </div>
              <div class="field">
                <span class="label">{{ u("preRanking") }}</span>
                <UiSelect v-model="form.preRankingMode" :options="preOptions" :label="u('preRanking')">
                  <template v-if="someUnconfigured" #footer><div class="opt-sep"></div><RouterLink class="opt" to="/settings/models"><Icon name="settings" />{{ u("goConfigure") }}</RouterLink></template>
                </UiSelect>
              </div>
            </div>
            <label v-if="supportsCitation" class="row" style="justify-content: space-between; gap: 16px; flex-wrap: nowrap; cursor: pointer">
              <span class="stack"><span class="label">{{ u("citationExpansion") }}</span><span class="help" style="margin-top: 4px">{{ u("citationExpansionHelp") }}</span></span>
              <span class="switch"><input v-model="form.citationEnabled" type="checkbox" role="switch" :aria-label="u('citationExpansion')" /><span class="track"><span class="thumb"></span></span></span>
            </label>
            <p class="xs muted">{{ u("defaultsNote") }}<RouterLink class="link" to="/settings/models">{{ u("editDefaults") }}</RouterLink></p>
          </div>
        </details>
        <div v-if="submitError" class="alert" role="alert" style="grid-template-columns: 16px 1fr">
          <Icon name="fail" />
          <p class="alert-title">{{ errorText(submitError) }}</p>
        </div>
      </div>
      <div class="cf">
        <button class="btn btn-ghost btn-sm" type="button" :disabled="!changed" @click="reset">{{ u("resetDefaults") }}</button>
        <div class="row" style="gap: 10px">
          <slot name="cost" />
          <span v-if="blocker && (needsSetup || activeRun)" class="xs muted">{{ blocker }}</span>
          <span v-else class="row" style="gap: 3px" aria-hidden="true"><span class="kbd">Ctrl</span><span class="kbd">Enter</span></span>
          <button class="btn btn-sm" type="submit" :disabled="!canSubmit" :title="blocker || undefined">
            <Icon v-if="busy" name="loader" />{{ busy ? u("submitting") : u("startSearch") }}
          </button>
        </div>
      </div>
    </form>
  </main>
</template>
