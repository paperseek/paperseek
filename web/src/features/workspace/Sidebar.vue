<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useRoute } from "vue-router";
import Icon, { type IconName } from "../../components/Icon.vue";
import { useUi } from "../../locales/ui";
import { runState } from "../../state/run";
import { reviewDraft } from "../review/review-draft";
import { currentModule, invalidateHistory, listPaths, modulePaths, type WorkspaceModule } from "./state";
import { useHistory } from "./use-history";
import type { HistoryRecord } from "./history-api";
import WorkspaceSettingsMenu from "./WorkspaceSettingsMenu.vue";

const props = defineProps<{ visible: boolean; historyEnabled?: boolean }>();
const emit = defineEmits<{ navigate: [] }>();
const route = useRoute();
const u = useUi();
const settings = computed(() => route.name === "settings");
const module = computed(() => currentModule(route));
const { page, load, search } = useHistory(module, computed(() => props.visible && props.historyEnabled !== false));
const searching = ref(false);
const searchInput = ref<HTMLInputElement>();
const modules: Array<{ id: WorkspaceModule; icon: IconName; label: "search" | "bibliometrics" | "reviews" }> = [
  { id: "search", icon: "search", label: "search" },
  { id: "bibliometrics", icon: "chart", label: "bibliometrics" },
  { id: "reviews", icon: "file-text", label: "reviews" },
];
const newLabel = computed(() => u(module.value === "search" ? "newSearch" : module.value === "bibliometrics" ? "newAnalysis" : "newReview"));

watch(() => runState.run?.status, (status, previous) => {
  if (previous && status !== previous) invalidateHistory("search");
});
watch(module, () => {
  searching.value = false;
});
async function toggleSearch() {
  searching.value = !searching.value;
  if (!searching.value && page.value.query) search("");
  await nextTick();
  searchInput.value?.focus();
}

const activeRun = computed(() => {
  const run = runState.run;
  if (module.value !== "search" || !run || !["queued", "running", "cancelling"].includes(run.status)) return null;
  if (page.value.query || page.value.items.some((item) => item.id === run.id)) return null;
  const record: HistoryRecord = {
    id: run.id, title: run.display_title || run.question, path: `/runs/${run.id}`, date: run.created_at,
    count: 0, status: run.status, active: true,
  };
  return record;
});
const records = computed(() => (activeRun.value ? [activeRun.value, ...page.value.items] : page.value.items));
const draftTitle = computed(() => reviewDraft.form.topic || reviewDraft.source?.title || "");
const showDraft = computed(() => module.value === "reviews" && reviewDraft.source && !reviewDraft.created
  && draftTitle.value.toLocaleLowerCase().includes(page.value.query.toLocaleLowerCase()));
const draftStatus = computed(() => u(reviewDraft.suggesting ? "reviewSelectionRunning"
  : reviewDraft.selection === "failed" ? "reviewSelectionFailed"
  : reviewDraft.selected.size ? "reviewSelectionReady" : "reviewSelectionDraft"));

function status(record: HistoryRecord): string {
  if (module.value === "search" && runState.run?.id === record.id) return runState.run.status;
  return record.status;
}
function icon(record: HistoryRecord): IconName {
  const value = status(record);
  if (module.value === "search") {
    return ({ queued: "clock", running: "loader", cancelling: "loader", completed_with_warnings: "warn",
      failed: "fail", cancelled: "circle-minus" } as Record<string, IconName>)[value] || "text-search";
  }
  if (module.value === "bibliometrics") return "chart";
  if (record.active) return "loader";
  return value === "reviewGeneratedNeedsCheck" || value === "reviewPhase_needs_attention" ? "warn" : "file";
}
function badge(record: HistoryRecord): { text: string; muted: boolean } {
  const value = status(record);
  if (module.value === "search" && ["queued", "running", "cancelling", "failed", "cancelled"].includes(value))
    return { text: u(`st_${value}` as "st_queued"), muted: true };
  return { text: record.count.toLocaleString(), muted: false };
}
function path(record: HistoryRecord): string {
  return module.value === "search" && ["completed", "completed_with_warnings"].includes(status(record))
    ? `${record.path}/results`
    : record.path;
}
function current(record: HistoryRecord): boolean {
  return route.path === record.path || route.path.startsWith(`${record.path}/`);
}
</script>

<template>
  <aside class="sb" :aria-label="u('nav')">
    <div class="sb-head">
      <RouterLink class="sb-brand" to="/search" aria-label="PaperSeek" @click="emit('navigate')"><img src="/paperseek-lockup.svg" alt="PaperSeek" style="height: 22px; width: auto; display: block" /></RouterLink>
    </div>
    <div class="sb-group" style="gap: 8px">
      <div class="sb-new">
        <RouterLink class="sb-btn" :to="modulePaths[module]" @click="emit('navigate')"><Icon name="circle-plus" />{{ newLabel }}</RouterLink>
      </div>
      <nav class="stack" style="gap: 4px" :aria-label="u('modules')">
        <RouterLink
          v-for="item in modules"
          :key="item.id"
          class="sb-btn"
          :class="{ active: !settings && module === item.id }"
          :to="modulePaths[item.id]"
          :aria-current="!settings && module === item.id ? 'page' : undefined"
          @click="emit('navigate')"
          ><Icon :name="item.icon" />{{ u(item.label) }}<span v-if="item.id === 'reviews'" class="sb-badge muted">{{ u("experimental") }}</span></RouterLink
        >
      </nav>
    </div>
    <div v-if="historyEnabled !== false" class="sb-group sb-scroll">
      <div class="sb-label">
        <span>{{ u(`history_${module}`) }}</span>
        <span class="row" style="gap: 2px; flex-wrap: nowrap">
          <button type="button" :aria-label="u('searchRecords')" :title="u('searchRecords')" :aria-pressed="searching" style="padding: 2px 4px; display: inline-flex" @click="toggleSearch"><Icon name="search" size="sm" /></button>
          <RouterLink :to="listPaths[module]" @click="emit('navigate')">{{ u("all") }}</RouterLink>
        </span>
      </div>
      <div v-if="searching" class="sb-search">
        <div class="ig">
          <input
            ref="searchInput"
            type="search"
            maxlength="200"
            :value="page.query"
            :placeholder="u('searchRecordTitles')"
            :aria-label="u('searchRecords')"
            @input="search(($event.target as HTMLInputElement).value)"
            @keydown.esc="toggleSearch"
          />
        </div>
      </div>
      <RouterLink v-if="showDraft" class="sb-btn" :class="{ active: route.path === '/reviews/new' }"
        to="/reviews/new" :title="draftTitle" :aria-current="route.path === '/reviews/new' ? 'page' : undefined" @click="emit('navigate')">
        <Icon :name="reviewDraft.suggesting ? 'loader' : 'file'" /><span>{{ draftTitle }}</span><span class="sb-badge muted">{{ draftStatus }}</span>
      </RouterLink>
      <RouterLink
        v-for="record in records"
        :key="record.id"
        class="sb-btn"
        :class="{ active: current(record) }"
        :to="path(record)"
        :title="record.title"
        :aria-current="current(record) ? 'page' : undefined"
        @click="emit('navigate')"
        ><Icon :name="icon(record)" /><span>{{ record.title }}</span><span class="sb-badge" :class="{ muted: badge(record).muted }">{{ badge(record).text }}</span></RouterLink
      >
      <p v-if="page.error" class="sb-empty" role="alert">{{ u("loadError") }} · <button type="button" class="link" @click="load()">{{ u("retry") }}</button></p>
      <p v-else-if="!showDraft && !records.length && !page.loading" class="sb-empty">{{ u(page.query ? "noMatches" : "noRecords") }}</p>
      <template v-if="page.loading && !records.length">
        <span v-for="index in 3" :key="index" class="skeleton" style="height: 20px; margin: 6px 8px"></span>
      </template>
      <button v-else-if="page.next !== null" type="button" class="sb-btn sb-more" :disabled="page.loading" @click="load(true)">
        <Icon v-if="page.loading" name="loader" size="sm" />{{ u("loadMore") }}
      </button>
    </div>
    <div v-else-if="$slots['history-off']" class="sb-group sb-scroll">
      <div class="sb-label"><span>{{ u(`history_${module}`) }}</span></div>
      <slot name="history-off" :module="module" />
    </div>
    <div class="sb-group sb-foot">
      <RouterLink class="sb-btn" :class="{ active: settings }" to="/settings/models" :aria-current="settings ? 'page' : undefined" @click="emit('navigate')"><Icon name="settings" />{{ u("settings") }}</RouterLink>
      <a class="sb-btn" href="https://docs.paperseek.app/" target="_blank" rel="noopener"><Icon name="circle-help" />{{ u("docs") }}</a>
    </div>
    <div class="sb-head" style="padding-top: 0">
      <slot name="account"><WorkspaceSettingsMenu /></slot>
    </div>
  </aside>
</template>
