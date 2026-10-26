<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch, watchEffect } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "../../api/client";
import Icon from "../../components/Icon.vue";
import UiMenu from "../../components/UiMenu.vue";
import { useUi } from "../../locales/ui";
import { saveDownload } from "../review/api";
import { historyRevision, listPaths, setHeader } from "../workspace/state";
import { metricsApi, type Analysis } from "./api";
import { useMetricMessages } from "./messages";
import MetricDistribution from "./MetricDistribution.vue";
import MetricNetworkPanel from "./MetricNetworkPanel.vue";
import { formatNumber } from "../results/venue-labels";

const { m } = useMetricMessages();
const u = useUi();
const route = useRoute();
const router = useRouter();
const identity = computed(() => String(route.params.analysisId || ""));
const analysis = ref<Analysis | null>(null);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const steps = ["overview", "rankings", "keywords", "coupling", "quality"] as const;
const tab = computed(() => steps.find((step) => step === route.query.view) || "overview");
let request: AbortController | undefined;
let generation = 0;

watchEffect(() => setHeader(analysis.value?.title || u("bibliometrics"), { label: u("bibliometrics"), to: listPaths.bibliometrics }));

function report(e: unknown) {
  error.value = e instanceof ApiError && m(e.code) !== e.code ? e.code : "error";
}
async function load() {
  const own = ++generation;
  request?.abort();
  request = new AbortController();
  error.value = "";
  if (!identity.value) return;
  if (analysis.value?.id !== identity.value) analysis.value = null;
  loading.value = true;
  try {
    const value = await metricsApi.get(identity.value, request.signal);
    if (own === generation) analysis.value = value;
  } catch (e) {
    if (own === generation && !request.signal.aborted) report(e);
  } finally {
    if (own === generation) loading.value = false;
  }
}
watch([identity, () => historyRevision.bibliometrics], load, { immediate: true });
onBeforeUnmount(() => {
  generation++;
  request?.abort();
});
async function exportData(format: "csv" | "json") {
  busy.value = true;
  error.value = "";
  try {
    saveDownload(await metricsApi.export(identity.value, format));
  } catch (e) {
    report(e);
  } finally {
    busy.value = false;
  }
}
function go(step: (typeof steps)[number]) {
  void router.replace({ query: { ...route.query, view: step === "overview" ? undefined : step } });
}
function tabKey(event: KeyboardEvent, index: number) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? steps.length - 1 : (index + (event.key === "ArrowLeft" ? -1 : 1) + steps.length) % steps.length;
  (event.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  go(steps[next]);
}
const stats = computed(() => analysis.value?.statistics);
const casTiers = computed(() => (stats.value?.cas_tiers || []).map((item) => ({ ...item, label: u("casBadge", { n: item.label }) })));
const ccfRanks = computed(() => (stats.value?.ccf_ranks || []).map((item) => ({ ...item, label: u("ccfBadge", { rank: item.label }) })));
const ccfA = computed(() => stats.value?.ccf_ranks?.find((item) => item.label === "A")?.count ?? 0);
const hasCitations = computed(() => stats.value?.coverage.some((field) => field.field === "citations" && field.available > 0));
</script>

<template>
  <main class="page">
    <div v-if="error" class="alert" role="alert"><Icon name="fail" /><p class="alert-title">{{ m(error) }}</p><div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="load">{{ m("retry") }}</button></div></div>
    <template v-if="loading && !analysis">
      <div class="toolbar"><span class="skeleton" style="width: 360px; height: 36px; border-radius: 10px"></span></div>
      <div class="stats"><div v-for="index in 3" :key="index" class="card stat"><span class="skeleton" style="height: 14px; width: 40%"></span><span class="skeleton" style="height: 28px; width: 30%"></span></div></div>
    </template>
    <template v-else-if="analysis && stats">
      <div class="toolbar">
        <div class="tabs" role="tablist" :aria-label="u('bibliometrics')">
          <button
            v-for="(step, index) in steps"
            :id="`metric-tab-${step}`"
            :key="step"
            class="tab"
            :class="{ active: tab === step }"
            type="button"
            role="tab"
            :aria-selected="tab === step"
            :aria-controls="`metric-panel-${step}`"
            :tabindex="tab === step ? 0 : -1"
            @keydown="tabKey($event, index)"
            @click="go(step)"
          >{{ u(`tab_${step}`) }}</button>
        </div>
        <span class="sm muted row" style="gap: 4px; margin-left: 8px">
          <RouterLink class="link" :to="`/runs/${analysis.source_run_id}/results`">{{ u("sourceSearch") }}</RouterLink>
          · {{ u("papersShort", { n: analysis.paper_count.toLocaleString() }) }}
          <template v-if="analysis.source_reported_total != null"> · {{ u("reportedTotal", { n: analysis.source_reported_total.toLocaleString() }) }}</template>
          <button class="btn btn-ghost btn-icon btn-xs" type="button" :aria-label="u('scopeInfo')" :title="m('corpusHint')" style="width: 24px; height: 24px"><Icon name="info" size="sm" /></button>
        </span>
        <div class="row end">
          <UiMenu :label="m('export')" trigger-class="btn btn-outline btn-sm" align="end" :min-width="180" :disabled="busy">
            <template #trigger><Icon name="download" />{{ m("export") }}<Icon name="chevron-down" /></template>
            <button class="opt" role="menuitem" data-menu-item type="button" @click="exportData('csv')">{{ m("exportCsv") }}</button>
            <button class="opt" role="menuitem" data-menu-item type="button" @click="exportData('json')">{{ m("exportJson") }}</button>
          </UiMenu>
        </div>
      </div>
      <div v-if="analysis.scope === 'legacy_ranked_only'" class="alert" style="grid-template-columns: 16px 1fr"><Icon name="warn" /><p class="alert-title">{{ m("legacy_ranked_only") }}</p></div>

      <section v-if="tab === 'overview'" id="metric-panel-overview" class="stack" style="gap: 16px" role="tabpanel" aria-labelledby="metric-tab-overview">
        <div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))">
          <div class="card stat"><p class="stat-label">{{ m("papers") }}</p><p class="stat-value">{{ stats.paper_count.toLocaleString() }}</p><p class="stat-foot">{{ u("statPapersFoot") }}</p></div>
          <div class="card stat"><p class="stat-label">{{ m("scored") }}</p><p class="stat-value">{{ stats.scored_count.toLocaleString() }}</p><p class="stat-foot">{{ u("statScoredFoot") }}</p></div>
          <div class="card stat"><p class="stat-label">{{ m("citations") }}</p><p class="stat-value">{{ hasCitations ? stats.citation_sum.toLocaleString() : "—" }}</p><p class="stat-foot">{{ u("statCitationsFoot") }}</p></div>
        </div>
        <div class="grid-2">
          <MetricDistribution :title="m('years')" :items="stats.years" chronological />
          <MetricDistribution :title="m('venues')" :items="stats.venues" :total="stats.category_totals?.venue" :limit="7" />
        </div>
        <div class="grid-4">
          <MetricDistribution :title="m('document_types')" :items="stats.document_types" />
          <MetricDistribution v-for="field in ['authors', 'institutions', 'countries'] as const" :key="field" :title="m(field)" :items="stats[field]" :total="stats.category_totals?.[field]" />
        </div>
        <p class="xs muted">{{ m("nameHint") }}</p>
      </section>

      <section v-else-if="tab === 'rankings'" id="metric-panel-rankings" class="stack" style="gap: 16px" role="tabpanel" aria-labelledby="metric-tab-rankings">
        <div v-if="!stats.ranked_paper_count" class="alert" style="grid-template-columns: 16px 1fr"><Icon name="info" /><p class="alert-title">{{ u("noRankings") }}</p></div>
        <template v-else>
          <div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))">
            <div class="card stat"><p class="stat-label">{{ u("rankedPapers") }}</p><p class="stat-value">{{ (stats.ranked_paper_count ?? 0).toLocaleString() }}</p><p class="stat-foot">{{ u("rankedPapersFoot", { n: stats.paper_count.toLocaleString() }) }}</p></div>
            <div class="card stat"><p class="stat-label">{{ u("casTopPapers") }}</p><p class="stat-value">{{ (stats.cas_top_count ?? 0).toLocaleString() }}</p></div>
            <div class="card stat"><p class="stat-label">{{ u("ccfAPapers") }}</p><p class="stat-value">{{ ccfA.toLocaleString() }}</p></div>
          </div>
          <div class="grid-3" style="display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr))">
            <MetricDistribution :title="u('casTiers')" :items="casTiers" />
            <MetricDistribution :title="u('jcrQuartiles')" :items="stats.jcr_quartiles || []" />
            <MetricDistribution :title="u('ccfRanks')" :items="ccfRanks" />
          </div>
          <section class="card c">
            <div class="card-header"><div><h2 class="card-title">{{ u("rankedVenues") }}</h2><p v-if="(stats.category_totals?.ranked_venues ?? 0) > (stats.ranked_venues?.length ?? 0)" class="card-desc">{{ u("topN", { n: stats.ranked_venues?.length ?? 0, total: (stats.category_totals?.ranked_venues ?? 0).toLocaleString() }) }}</p></div></div>
            <div class="table-wrap" style="border: 0">
              <table class="table">
                <thead><tr><th>{{ u("colVenue") }}</th><th class="shrink r">{{ u("colRankedPapers") }}</th><th class="shrink">CCF</th><th class="shrink">{{ u("casTiers") }}</th><th class="shrink">JCR</th><th class="shrink r">IF</th></tr></thead>
                <tbody>
                  <tr v-for="venue in stats.ranked_venues" :key="venue.label">
                    <td class="wrap">{{ venue.label }}</td>
                    <td class="r num">{{ venue.count.toLocaleString() }}</td>
                    <td>{{ venue.ccf_rank || "—" }}</td>
                    <td class="nowrap">{{ venue.cas_tier ? u("casBadge", { n: venue.cas_tier }) + (venue.cas_top ? ` · ${u("casTop")}` : "") : "—" }}</td>
                    <td>{{ venue.jcr_quartile || "—" }}</td>
                    <td class="r num">{{ formatNumber(venue.impact_factor) || "—" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </template>
        <p class="xs muted">{{ u("rankingsHint") }}</p>
      </section>

      <section v-else-if="tab === 'keywords'" id="metric-panel-keywords" class="stack" style="gap: 16px" role="tabpanel" aria-labelledby="metric-tab-keywords">
        <MetricNetworkPanel :analysis-id="identity" :source-run-id="analysis.source_run_id" kind="keywords" />
        <MetricDistribution :title="u('topKeywords')" :items="stats.keywords" :total="stats.category_totals?.keywords" columns />
        <p class="xs muted">{{ m("languageHint") }}</p>
      </section>

      <section v-else-if="tab === 'coupling'" id="metric-panel-coupling" class="stack" style="gap: 16px" role="tabpanel" aria-labelledby="metric-tab-coupling">
        <MetricNetworkPanel :analysis-id="identity" :source-run-id="analysis.source_run_id" kind="coupling" />
      </section>

      <section v-else id="metric-panel-quality" class="stack" style="gap: 16px" role="tabpanel" aria-labelledby="metric-tab-quality">
        <div class="table-wrap">
          <table class="table">
            <thead><tr><th>{{ m("field") }}</th><th class="shrink r">{{ m("available") }}</th><th style="width: 40%">{{ m("coverage") }}</th><th class="shrink r">{{ m("missing") }}</th></tr></thead>
            <tbody>
              <tr v-for="field in stats.coverage" :key="field.field">
                <td>{{ m(field.field === "citations" ? "citationCount" : field.field) }}</td>
                <td class="r num">{{ field.available.toLocaleString() }} / {{ field.total.toLocaleString() }}</td>
                <td><span class="row" style="gap: 10px; flex-wrap: nowrap"><span class="meter grow"><span :style="{ width: `${(100 * field.available) / (field.total || 1)}%` }"></span></span><span class="xs muted num" style="width: 44px; text-align: right">{{ ((100 * field.available) / (field.total || 1)).toFixed(1) }}%</span></span></td>
                <td class="r num muted">{{ (field.total - field.available).toLocaleString() }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="grid-2">
          <MetricDistribution :title="m('keyword_sources')" :items="stats.keyword_sources" />
          <MetricDistribution :title="m('citation_sources')" :items="stats.citation_sources" />
        </div>
        <p class="xs muted">{{ m("qualityHint") }}</p>
        <ul v-if="analysis.warnings?.length" class="stack" style="gap: 4px"><li v-for="warning in analysis.warnings" :key="warning" class="xs muted">{{ m(warning) }}</li></ul>
      </section>
    </template>
  </main>
</template>
