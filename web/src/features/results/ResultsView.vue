<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch, watchEffect } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, type ResultPageQuery } from "../../api/client";
import type { PaperResult, RunDetail } from "../../api/types";
import Icon from "../../components/Icon.vue";
import UiMenu from "../../components/UiMenu.vue";
import UiSelect, { type SelectOption } from "../../components/UiSelect.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { MAX_REVIEW_PAPERS } from "../review/api";
import RunSettingsDialog from "../search/RunSettingsDialog.vue";
import RunToolbar from "../search/RunToolbar.vue";
import { useSearchConfiguration } from "../search/use-search-configuration";
import { loadSettings, workspaceSettings } from "../settings/store";
import { listPaths, setHeader } from "../workspace/state";
import ResultCard from "./ResultCard.vue";
import { recallResults, rememberResults } from "./view-state";

const props = defineProps<{ snapshot?: { run: RunDetail; results: PaperResult[] }; reviewsEnabled?: boolean }>();
const { runMessage, t } = useLocale();
const u = useUi();
const route = useRoute();
const router = useRouter();
const runId = computed(() => String(route.params.runId || ""));
const run = ref<RunDetail | null>(null);
const results = ref<PaperResult[]>([]);
const nextCursor = ref<string | null>(null);
const pageState = ref<"loading" | "error" | "loaded">("loading");
const loadingMore = ref(false);
const loadMoreFailed = ref(false);
const refreshing = ref(false);
const refreshFailed = ref(false);
const query = ref("");
const appliedQuery = ref("");
const minScore = ref("all");
const availability = ref("all");
const sort = ref<NonNullable<ResultPageQuery["sort"]>>("rank");
const selectedIds = ref(new Set<string>());
const uidById = new Map<string, string>();
const expanded = ref(new Set<string>());
const exporting = ref(false);
const exportError = ref("");
const totalCount = ref<number | null>(null);
const matchedCount = ref<number | null>(null);
const scoredCount = ref<number | null>(null);
const maxScore = ref<number | null>(null);
let generation = 0;
let shownRunId = "";
let controller: AbortController | null = null;
let queryTimer: ReturnType<typeof setTimeout> | null = null;
const { restoreRun } = useSearchConfiguration(computed(() => workspaceSettings.descriptors));

watchEffect(() => setHeader(run.value?.display_title || run.value?.question || u("tabResults"), { label: u("search"), to: listPaths.search }));

const paperId = (paper: PaperResult) => String(paper.uid || paper.doi || paper.url || paper.title || paper.rank);
function rememberUids(papers: PaperResult[]) {
  for (const paper of papers) if (paper.uid) uidById.set(paperId(paper), paper.uid);
}
const filtersActive = computed(() => Boolean(appliedQuery.value.trim()) || minScore.value !== "all" || availability.value !== "all");
const total = computed(() => totalCount.value ?? run.value?.result_count ?? results.value.length);
const summaryLine = computed(() => {
  const parts = [u("summary", { total: total.value.toLocaleString() })];
  if (filtersActive.value && matchedCount.value !== null) parts.push(u("summaryMatched", { n: matchedCount.value }));
  if (scoredCount.value !== null) parts.push(u("summaryScored", { n: scoredCount.value }));
  if ((scoredCount.value ?? 0) > 0 && maxScore.value !== null) parts.push(u("summaryMax", { n: maxScore.value }));
  return parts.join(" · ");
});
const rankingBanner = computed(() => (run.value?.warnings || []).some((warning) => warning.startsWith("ranking_") && warning.endsWith("local_order_retained")));
const otherWarnings = computed(() => (run.value?.warnings || []).filter((warning) => !(warning.startsWith("ranking_") && warning.endsWith("local_order_retained"))));
const selectedUids = computed(() => [...selectedIds.value].map((id) => uidById.get(id)).filter((uid): uid is string => Boolean(uid)));
const hiddenSelected = computed(() => {
  const shown = new Set(results.value.map(paperId));
  return [...selectedIds.value].filter((id) => !shown.has(id)).length;
});
const loadedSelected = computed(() => results.value.filter((paper) => selectedIds.value.has(paperId(paper))).length);
const allLoaded = computed(() => results.value.length > 0 && loadedSelected.value === results.value.length);
const completed = computed(() => ["completed", "completed_with_warnings"].includes(run.value?.status || ""));
const canReview = computed(() => completed.value && selectedUids.value.length >= 2 && selectedUids.value.length <= MAX_REVIEW_PAPERS);

const scoreOptions = computed<SelectOption<string>[]>(() => [
  { value: "all", label: u("scoreAll") },
  ...["7", "8", "9"].map((value) => ({ value, label: u("scoreAtLeast", { n: value }) })),
]);
const metaOptions = computed<SelectOption<string>[]>(() => [
  { value: "all", label: u("metaAll") },
  { value: "abstract", label: u("metaAbstract") },
  { value: "doi", label: u("metaDoi") },
  { value: "pdf", label: u("metaPdf") },
]);
const sortOptions = computed<SelectOption<NonNullable<ResultPageQuery["sort"]>>[]>(() => [
  { value: "rank", label: u("sortRank") },
  { value: "score", label: u("sortScore") },
  { value: "citations", label: u("sortCitations") },
  { value: "year", label: u("sortYear") },
]);

function toggleSelection(paper: PaperResult) {
  const next = new Set(selectedIds.value);
  const id = paperId(paper);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedIds.value = next;
}
function toggleAllLoaded() {
  const next = new Set(selectedIds.value);
  for (const paper of results.value) {
    if (allLoaded.value) next.delete(paperId(paper));
    else next.add(paperId(paper));
  }
  selectedIds.value = next;
}
function toggleExpanded(id: string) {
  const next = new Set(expanded.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expanded.value = next;
}
function filterParams(): ResultPageQuery {
  return {
    query: appliedQuery.value.trim() || undefined,
    minScore: minScore.value === "all" ? null : Number(minScore.value),
    metadata: availability.value === "all" ? "" : (availability.value as ResultPageQuery["metadata"]),
    sort: sort.value,
  };
}
function applySummary(page: { total_count?: number; matched_count?: number; scored_count?: number; max_score?: number | null }) {
  totalCount.value = page.total_count ?? null;
  matchedCount.value = page.matched_count ?? null;
  scoredCount.value = page.scored_count ?? null;
  maxScore.value = page.max_score ?? null;
}
// Filters, search and sort live in the URL so reload, back and shared links keep them.
function syncUrl() {
  const next: Record<string, string> = {};
  if (appliedQuery.value.trim()) next.q = appliedQuery.value.trim();
  if (minScore.value !== "all") next.score = minScore.value;
  if (availability.value !== "all") next.meta = availability.value;
  if (sort.value !== "rank") next.sort = sort.value;
  void router.replace({ query: next });
}
async function applyFilters() {
  const value = runId.value;
  if (!value || pageState.value === "loading") return;
  syncUrl();
  const current = ++generation;
  controller?.abort();
  controller = new AbortController();
  refreshing.value = true;
  refreshFailed.value = false;
  loadMoreFailed.value = false;
  try {
    const page = await api.resultPage(value, filterParams(), controller.signal);
    if (current !== generation || runId.value !== value) return;
    results.value = page.results || [];
    rememberUids(results.value);
    nextCursor.value = page.next_cursor ?? null;
    applySummary(page);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") return;
    if (current === generation) refreshFailed.value = true;
  } finally {
    if (current === generation) refreshing.value = false;
  }
}
async function loadMore() {
  if (!nextCursor.value || loadingMore.value) return;
  const value = runId.value;
  const current = generation;
  loadingMore.value = true;
  loadMoreFailed.value = false;
  try {
    const page = await api.resultPage(value, { ...filterParams(), cursor: nextCursor.value }, controller?.signal);
    if (current === generation && value === runId.value) {
      const known = new Set(results.value.map(paperId));
      const incoming = (page.results || []).filter((paper) => !known.has(paperId(paper)));
      rememberUids(incoming);
      results.value = [...results.value, ...incoming];
      nextCursor.value = page.next_cursor ?? null;
    }
  } catch {
    if (current === generation) loadMoreFailed.value = true;
  } finally {
    if (current === generation) loadingMore.value = false;
  }
}
async function load(value: string) {
  if (!value) return;
  const current = ++generation;
  controller?.abort();
  controller = new AbortController();
  run.value = null;
  results.value = [];
  nextCursor.value = null;
  exportError.value = "";
  pageState.value = "loading";
  if (props.snapshot?.run.id === value) {
    run.value = props.snapshot.run;
    results.value = props.snapshot.results;
    rememberUids(results.value);
    pageState.value = "loaded";
    return;
  }
  const [detail, page] = await Promise.allSettled([api.run(value, controller.signal), api.resultPage(value, filterParams(), controller.signal)]);
  if (current !== generation || runId.value !== value) return;
  if (detail.status === "fulfilled") run.value = detail.value;
  if (page.status !== "fulfilled") {
    pageState.value = "error";
    return;
  }
  results.value = page.value.results || [];
  rememberUids(results.value);
  nextCursor.value = page.value.next_cursor ?? null;
  applySummary(page.value);
  pageState.value = "loaded";
  const count = recallResults(value)?.loadedCount || 0;
  while (current === generation && results.value.length < count && nextCursor.value) {
    const cursor = nextCursor.value;
    await loadMore();
    if (loadMoreFailed.value || cursor === nextCursor.value) break;
  }
}
function remember(id: string) {
  if (!id || props.snapshot) return;
  rememberResults(id, {
    query: query.value, minScore: minScore.value, availability: availability.value, sort: sort.value,
    selected: [...selectedIds.value], uids: [...uidById], expanded: [...expanded.value], loadedCount: results.value.length,
  });
}
watch(runId, (value) => {
  if (!value) return;
  if (shownRunId) remember(shownRunId);
  shownRunId = value;
  pageState.value = "loading";
  const saved = props.snapshot ? undefined : recallResults(value);
  const fromUrl = route.query;
  query.value = appliedQuery.value = typeof fromUrl.q === "string" ? fromUrl.q : saved?.query || "";
  minScore.value = typeof fromUrl.score === "string" && ["7", "8", "9"].includes(fromUrl.score) ? fromUrl.score : saved?.minScore || "all";
  availability.value = typeof fromUrl.meta === "string" && ["abstract", "doi", "pdf"].includes(fromUrl.meta) ? fromUrl.meta : saved?.availability || "all";
  sort.value = (typeof fromUrl.sort === "string" && ["score", "citations", "year"].includes(fromUrl.sort) ? fromUrl.sort : saved?.sort || "rank") as typeof sort.value;
  selectedIds.value = new Set(saved?.selected);
  expanded.value = new Set(saved?.expanded);
  uidById.clear();
  for (const [id, uid] of saved?.uids || []) uidById.set(id, uid);
  void load(value);
}, { immediate: true });
watch(query, (value) => {
  if (queryTimer) clearTimeout(queryTimer);
  if (pageState.value === "loading") return;
  queryTimer = setTimeout(() => {
    appliedQuery.value = value;
    void applyFilters();
  }, 180);
});
watch([minScore, availability, sort], () => void applyFilters());

async function exportSelected(format: "csv" | "json") {
  if (!selectedUids.value.length || exporting.value) return;
  exporting.value = true;
  exportError.value = "";
  try {
    const file = await api.exportSelected(runId.value, format, selectedUids.value);
    const url = URL.createObjectURL(file.blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.filename;
    anchor.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    exportError.value = error instanceof Error ? error.message : "export_failed";
  } finally {
    exporting.value = false;
  }
}
function clearFilters() {
  query.value = appliedQuery.value = "";
  minScore.value = "all";
  availability.value = "all";
  void applyFilters();
}
function createReview() {
  if (!canReview.value) return;
  void router.push({ path: "/reviews/new", query: { source: runId.value, paper: selectedUids.value } });
}
async function rerun() {
  await loadSettings();
  restoreRun(run.value);
  void router.push("/search");
}
onBeforeUnmount(() => {
  remember(shownRunId);
  generation += 1;
  controller?.abort();
  if (queryTimer) clearTimeout(queryTimer);
});
</script>

<template>
  <main class="page">
    <RunToolbar :run="run" tab="results" :result-count="totalCount">
      <RunSettingsDialog :run="run" :can-rerun="!snapshot" @rerun="rerun" />
      <RouterLink v-if="!snapshot && completed" class="btn btn-outline btn-sm" :to="{ path: '/bibliometrics', query: { source: runId } }"><Icon name="chart" />{{ u("analyze") }}</RouterLink>
      <UiMenu :label="u('exportAll')" trigger-class="btn btn-outline btn-sm" align="end" :min-width="160">
        <template #trigger><Icon name="download" />{{ u("exportAll") }}<Icon name="chevron-down" /></template>
        <slot name="exports" :run-id="runId" :selected-uids="selectedUids">
          <a class="opt" role="menuitem" data-menu-item :href="api.exportUrl(runId, 'csv')">CSV</a>
          <a class="opt" role="menuitem" data-menu-item :href="api.exportUrl(runId, 'json')">JSON</a>
        </slot>
      </UiMenu>
    </RunToolbar>

    <div class="ig lg">
      <Icon name="search" />
      <input v-model="query" type="search" :placeholder="u('searchInResults')" :aria-label="u('searchInResults')" />
    </div>
    <div class="toolbar" :aria-label="t('resultControls')">
      <label class="cb sm btn btn-outline btn-sm" style="font-weight: 500">
        <input type="checkbox" :checked="allLoaded" :aria-checked="loadedSelected && !allLoaded ? 'mixed' : allLoaded" :disabled="!results.length" @change="toggleAllLoaded" />
        <span class="box" :class="{ mixed: loadedSelected > 0 && !allLoaded }"><Icon :name="loadedSelected > 0 && !allLoaded ? 'minus' : 'check'" /></span>{{ u("selectAllLoaded") }}
      </label>
      <UiSelect v-model="minScore" :options="scoreOptions" :label="u('score')" :prefix="u('score')" size="sm" inline chevron="down" />
      <UiSelect v-model="availability" :options="metaOptions" :label="u('metadata')" :prefix="u('metadata')" size="sm" inline chevron="down" />
      <div class="row end" style="gap: 8px">
        <span class="sm muted num" data-testid="results-summary">{{ summaryLine }}<template v-if="refreshing"> · {{ u("loading") }}</template></span>
        <UiSelect v-model="sort" :options="sortOptions" :label="u('sortLabel')" size="sm" inline chevron="down" width="128px" />
      </div>
    </div>

    <div v-if="rankingBanner" class="alert" role="status" data-testid="ranking-banner" style="grid-template-columns: 16px 1fr"><Icon name="warn" /><p class="alert-title">{{ u("rankingDegraded") }}</p></div>
    <div v-if="otherWarnings.length" class="alert" data-testid="results-warnings" style="grid-template-columns: 16px 1fr">
      <Icon name="warn" /><p class="alert-title">{{ u("st_completed_with_warnings") }}</p>
      <ul class="alert-desc stack" style="gap: 2px"><li v-for="warning in otherWarnings" :key="warning">{{ runMessage(warning) }}</li></ul>
    </div>
    <div v-if="exportError" class="alert" role="alert" style="grid-template-columns: 16px 1fr"><Icon name="fail" /><p class="alert-title">{{ t("exportFailed") }}</p></div>
    <div v-if="refreshFailed" class="alert" role="alert"><Icon name="fail" /><p class="alert-title">{{ u("loadError") }}</p><div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="applyFilters">{{ u("retry") }}</button></div></div>

    <section v-if="pageState === 'loading'" class="stack" style="gap: 12px" data-testid="results-loading" aria-busy="true">
      <div v-for="index in 3" :key="index" class="card paper"><span class="skeleton" style="width: 16px; height: 16px"></span><div class="stack" style="gap: 8px"><span class="skeleton" style="height: 18px; width: 80%"></span><span class="skeleton" style="height: 14px; width: 50%"></span></div><span></span><div class="body"><span class="skeleton" style="height: 40px"></span></div></div>
    </section>
    <div v-else-if="pageState === 'error'" class="alert" role="alert"><Icon name="fail" /><p class="alert-title">{{ u("loadError") }}</p><div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="load(runId)">{{ u("retry") }}</button></div></div>
    <template v-else>
      <div v-if="!results.length && !filtersActive" class="empty">
        <span class="empty-media"><Icon name="text-search" size="lg" /></span>
        <p class="empty-title">{{ u("noResults") }}</p>
        <p class="empty-desc">{{ u("noResultsDesc") }}</p>
        <button class="btn btn-outline btn-sm" type="button" style="margin-top: 8px" @click="rerun">{{ u("rerun") }}</button>
      </div>
      <div v-else-if="!results.length" class="empty" data-testid="no-filtered-results">
        <span class="empty-media"><Icon name="search" size="lg" /></span>
        <p class="empty-title">{{ u("noFiltered") }}</p>
        <button class="btn btn-outline btn-sm" type="button" style="margin-top: 8px" @click="clearFilters">{{ u("clearFilters") }}</button>
      </div>
      <section v-else class="stack" style="gap: 12px" :aria-label="u('papers')">
        <ResultCard
          v-for="paper in results"
          :key="paperId(paper)"
          :paper="paper"
          :identity="paperId(paper)"
          :selected="selectedIds.has(paperId(paper))"
          :expanded="expanded"
          @select="toggleSelection(paper)"
          @expand="toggleExpanded"
        />
      </section>
      <div v-if="results.length" class="row" style="justify-content: center; gap: 12px">
        <span class="xs muted num">{{ u("loaded", { n: results.length.toLocaleString(), total: (filtersActive && matchedCount !== null ? matchedCount : total).toLocaleString() }) }}</span>
        <button v-if="nextCursor" class="btn btn-outline btn-sm" type="button" :disabled="loadingMore" @click="loadMore"><Icon v-if="loadingMore" name="loader" />{{ u("loadMore") }}</button>
        <button v-if="loadMoreFailed" class="btn btn-ghost btn-sm" type="button" role="alert" @click="loadMore">{{ u("loadError") }} · {{ u("retry") }}</button>
      </div>
    </template>

    <div v-if="selectedIds.size" class="bulk" role="region" :aria-label="u('selectedN', { n: selectedIds.size })">
      <span class="sm medium num" data-testid="selection-count">{{ u("selectedN", { n: selectedIds.size }) }}</span>
      <span v-if="hiddenSelected" class="xs" style="opacity: .7" data-testid="selection-out-of-filter">{{ u("hiddenSelected", { n: hiddenSelected }) }}</span>
      <span class="div"></span>
      <UiMenu :label="u('exportSelected')" trigger-class="btn" side="top" :min-width="140" :disabled="exporting || !selectedUids.length">
        <template #trigger><Icon :name="exporting ? 'loader' : 'download'" />{{ u("exportSelected") }}</template>
        <button class="opt" role="menuitem" data-menu-item type="button" data-testid="export-selected-csv" @click="exportSelected('csv')">CSV</button>
        <button class="opt" role="menuitem" data-menu-item type="button" data-testid="export-selected-json" @click="exportSelected('json')">JSON</button>
      </UiMenu>
      <span v-if="reviewsEnabled" :title="canReview ? undefined : u('reviewSelectionRule')"><button class="btn light" type="button" :disabled="!canReview" @click="createReview"><Icon name="file-plus" />{{ u("createReviewFromSelection") }}</button></span>
      <button class="btn btn-icon" type="button" :aria-label="u('clearSelectionLabel')" style="width: 32px; padding: 0" @click="selectedIds = new Set()"><Icon name="x" /></button>
    </div>
  </main>
</template>
