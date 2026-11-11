<script setup lang="ts">
// Analysis and review record tables (search history has its own view).
import { computed, onBeforeUnmount, ref, watch, watchEffect } from "vue";
import Icon, { type IconName } from "../../components/Icon.vue";
import { metricsApi, type AnalysisPage } from "../bibliometrics/api";
import { reviewApi, type ReviewPage } from "../review/api";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { useDates } from "./format";
import RecordMenu from "./RecordMenu.vue";
import { historyRevision, modulePaths, setHeader } from "./state";

const props = defineProps<{ module: "bibliometrics" | "reviews" }>();
const u = useUi();
const { t } = useLocale();
const { short } = useDates();
type Analysis = AnalysisPage["items"][number];
type Review = ReviewPage["items"][number];
const analyses = ref<Analysis[]>([]);
const reviews = ref<Review[]>([]);
const total = ref<number | null>(null);
const next = ref<number | null>(null);
const query = ref("");
const loading = ref(true);
const loadingMore = ref(false);
const error = ref(false);
let controller: AbortController | undefined;
let timer: ReturnType<typeof setTimeout> | undefined;
let generation = 0;

watchEffect(() => setHeader(u(props.module)));
const count = computed(() => (props.module === "bibliometrics" ? analyses.value.length : reviews.value.length));

async function load(more = false) {
  const own = ++generation;
  controller?.abort();
  controller = new AbortController();
  if (more) loadingMore.value = true;
  else loading.value = true;
  error.value = false;
  try {
    const offset = more ? next.value || 0 : 0;
    if (props.module === "bibliometrics") {
      const page = await metricsApi.list(query.value.trim(), offset, controller.signal);
      if (own !== generation) return;
      analyses.value = more ? [...analyses.value, ...page.items] : page.items;
      total.value = page.total_count;
      next.value = page.next_offset ?? null;
    } else {
      const page = await reviewApi.list(offset, query.value.trim(), controller.signal);
      if (own !== generation) return;
      reviews.value = more ? [...reviews.value, ...page.items] : page.items;
      total.value = page.total_count;
      next.value = page.next_offset ?? null;
    }
  } catch (e) {
    if (own === generation && !(e instanceof Error && e.name === "AbortError")) error.value = true;
  } finally {
    if (own === generation) loading.value = loadingMore.value = false;
  }
}
watch(query, () => {
  clearTimeout(timer);
  timer = setTimeout(() => void load(), 200);
});
watch(() => [props.module, historyRevision[props.module]], () => void load(), { immediate: true });
onBeforeUnmount(() => {
  generation++;
  controller?.abort();
  clearTimeout(timer);
});

function reviewStatus(item: Review): { icon: IconName | null; label: string } {
  if (item.active) return { icon: "loader", label: u("st_running") };
  if (item.phase === "needs_attention") return { icon: "warn", label: t("reviewPhase_needs_attention") };
  if (item.phase === "complete") return { icon: "ok", label: t("reviewPhase_complete") };
  return { icon: null, label: t(`reviewPhase_${item.phase}`) };
}
function reviewRecord(item: Review) {
  return { id: item.id, title: item.display_title || item.topic, path: `/reviews/${item.id}`, date: item.updated_at, count: item.paper_count, status: item.phase, active: item.active };
}
function analysisRecord(item: Analysis) {
  return { id: item.id, title: item.title, path: `/bibliometrics/${item.id}`, date: item.created_at, count: item.paper_count, status: "", active: false };
}
</script>

<template>
  <main class="page">
    <div class="toolbar">
      <div class="ig" style="max-width: 360px">
        <Icon name="search" />
        <input v-model="query" type="search" maxlength="200" :placeholder="u('searchRecordTitles')" :aria-label="u('searchRecords')" />
      </div>
      <span v-if="!loading && total !== null" class="sm muted">{{ u(module === "bibliometrics" ? "totalAnalyses" : "totalReviews", { n: total.toLocaleString() }) }}</span>
      <div class="row end" style="gap: 8px">
        <button class="btn btn-ghost btn-icon btn-sm" type="button" :aria-label="u('refresh')" :title="u('refresh')" :disabled="loading" @click="load()"><Icon :name="loading ? 'loader' : 'refresh'" /></button>
        <RouterLink class="btn btn-sm" :to="modulePaths[module]"><Icon name="plus" />{{ u(module === "bibliometrics" ? "newAnalysis" : "newReview") }}</RouterLink>
      </div>
    </div>

    <div v-if="error" class="alert" role="alert"><Icon name="fail" /><p class="alert-title">{{ u("loadError") }}</p><div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="load()">{{ u("retry") }}</button></div></div>
    <div v-else-if="!loading && !count" class="empty">
      <span class="empty-media"><Icon :name="module === 'bibliometrics' ? 'chart' : 'file-text'" size="lg" /></span>
      <p class="empty-title">{{ query ? u("noMatches") : u(module === "bibliometrics" ? "emptyAnalyses" : "emptyReviews") }}</p>
      <template v-if="!query">
        <p class="empty-desc">{{ u(module === "bibliometrics" ? "emptyAnalysesDesc" : "emptyReviewsDesc") }}</p>
        <RouterLink class="btn btn-sm" style="margin-top: 8px" :to="modulePaths[module]"><Icon name="plus" />{{ u(module === "bibliometrics" ? "newAnalysis" : "newReview") }}</RouterLink>
      </template>
      <button v-else class="btn btn-outline btn-sm" style="margin-top: 8px" type="button" @click="query = ''">{{ u("clearFilters") }}</button>
    </div>
    <div v-else class="table-wrap">
      <table v-if="module === 'bibliometrics'" class="table">
        <thead>
          <tr><th>{{ u("colTitle") }}</th><th class="shrink r">{{ u("colPapers") }}</th><th class="shrink">{{ u("colSourceRun") }}</th><th class="shrink">{{ u("colCreated") }}</th><th class="shrink"><span class="sr-only">{{ u("colActions") }}</span></th></tr>
        </thead>
        <tbody v-if="loading">
          <tr v-for="index in 4" :key="index"><td><span class="skeleton" style="height: 16px; width: 60%"></span></td><td></td><td><span class="skeleton" style="height: 14px; width: 56px"></span></td><td><span class="skeleton" style="height: 14px; width: 72px"></span></td><td></td></tr>
        </tbody>
        <tbody v-else>
          <tr v-for="item in analyses" :key="item.id">
            <td><RouterLink class="medium" :to="`/bibliometrics/${item.id}`">{{ item.title }}</RouterLink></td>
            <td class="r num">{{ item.paper_count.toLocaleString() }}</td>
            <td><RouterLink class="link muted" :to="`/runs/${item.source_run_id}/results`">{{ u("viewSearch") }}</RouterLink></td>
            <td class="num muted">{{ short(item.created_at) }}</td>
            <td><RecordMenu module="bibliometrics" :record="analysisRecord(item)" /></td>
          </tr>
        </tbody>
      </table>
      <table v-else class="table">
        <thead>
          <tr><th>{{ u("colTitle") }}</th><th class="shrink r">{{ u("colReviewPapers") }}</th><th class="shrink">{{ u("colSections") }}</th><th class="shrink">{{ u("colStatus") }}</th><th class="shrink">{{ u("colUpdated") }}</th><th class="shrink"><span class="sr-only">{{ u("colActions") }}</span></th></tr>
        </thead>
        <tbody v-if="loading">
          <tr v-for="index in 4" :key="index"><td style="min-width: 300px"><span class="skeleton" style="height: 16px; width: 60%"></span></td><td></td><td></td><td><span class="skeleton" style="height: 18px; width: 56px"></span></td><td><span class="skeleton" style="height: 14px; width: 72px"></span></td><td></td></tr>
        </tbody>
        <tbody v-else>
          <tr v-for="item in reviews" :key="item.id">
            <td style="min-width: 300px"><RouterLink class="medium" :to="`/reviews/${item.id}`">{{ item.display_title || item.topic }}</RouterLink></td>
            <td class="r num">{{ item.paper_count.toLocaleString() }}</td>
            <td class="num" :class="{ muted: !item.section_count }">
              <template v-if="item.section_count">{{ u("sectionsDone", { done: item.completed_sections, total: item.section_count }) }}<template v-if="item.pending_sections"> · {{ u("sectionsPending", { n: item.pending_sections }) }}</template></template>
              <template v-else>—</template>
            </td>
            <td><span class="badge badge-outline muted"><Icon v-if="reviewStatus(item).icon" :name="reviewStatus(item).icon!" />{{ reviewStatus(item).label }}</span></td>
            <td class="num muted">{{ short(item.updated_at) }}</td>
            <td><RecordMenu module="reviews" :record="reviewRecord(item)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="next !== null && count" class="row" style="justify-content: center"><button class="btn btn-outline btn-sm" type="button" :disabled="loadingMore" @click="load(true)"><Icon v-if="loadingMore" name="loader" />{{ u("loadMore") }}</button></div>
  </main>
</template>
