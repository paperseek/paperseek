<script setup lang="ts">
// Inset sidebar shell (shadcn dashboard-01): sidebar, 48px header with breadcrumb
// and language, and the scrolling content inset.
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import Icon from "../../components/Icon.vue";
import UiMenu from "../../components/UiMenu.vue";
import Toaster from "../../components/Toaster.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import Sidebar from "./Sidebar.vue";
import { pageHeader, settingsReturn } from "./state";

const props = withDefaults(defineProps<{ historyEnabled?: boolean; historyKey?: string }>(), { historyEnabled: true });
const route = useRoute();
const u = useUi();
const { language, setLanguage } = useLocale();
const small = ref(false);
const open = ref(true);
const toggle = ref<HTMLButtonElement>();
const main = ref<HTMLElement>();
const content = ref<HTMLElement>();
let media: MediaQueryList;

const scrollPositions = new Map<string, number>();
let scrollObserver: ResizeObserver | undefined;
let restorePosition: number | null = null;
function stopRestoring() {
  scrollObserver?.disconnect();
  restorePosition = null;
}
function restoreScroll(position: number) {
  stopRestoring();
  if (!main.value || !content.value) return;
  restorePosition = position;
  const apply = () => {
    if (!main.value || restorePosition === null) return;
    main.value.scrollTop = restorePosition;
    if (Math.abs(main.value.scrollTop - restorePosition) < 2) stopRestoring();
  };
  scrollObserver = new ResizeObserver(apply);
  scrollObserver.observe(content.value);
  apply();
}
watch(() => props.historyKey, () => {
  scrollPositions.clear();
  stopRestoring();
});

function viewport() {
  small.value = media.matches;
  open.value = !small.value;
  if (!small.value) {
    try {
      open.value = localStorage.getItem("paperseek.sidebar.visible") !== "false";
    } catch {
      /* Private browsing. */
    }
  }
}
function toggleSidebar() {
  open.value = !open.value;
  if (!small.value) {
    try {
      localStorage.setItem("paperseek.sidebar.visible", String(open.value));
    } catch {
      /* Private browsing. */
    }
  }
}
function closeSheet() {
  if (!small.value) return;
  open.value = false;
  void nextTick(() => toggle.value?.focus());
}
function sheetKey(event: KeyboardEvent) {
  if (event.key === "Escape") closeSheet();
}
onMounted(() => {
  media = matchMedia("(max-width: 900px)");
  viewport();
  media.addEventListener("change", viewport);
});
onBeforeUnmount(() => {
  media?.removeEventListener("change", viewport);
  stopRestoring();
});
watch(() => route.fullPath, async (path, previous) => {
  scrollPositions.set(previous, restorePosition ?? main.value?.scrollTop ?? 0);
  if (scrollPositions.size > 40) scrollPositions.delete(scrollPositions.keys().next().value!);
  stopRestoring();
  if (path.startsWith("/settings/") && !previous.startsWith("/settings/")) {
    settingsReturn.path = previous === "/" ? "/search" : previous;
    settingsReturn.scroll = main.value?.scrollTop || 0;
  }
  closeSheet();
  await nextTick();
  restoreScroll(
    previous.startsWith("/settings/") && path === settingsReturn.path ? settingsReturn.scroll : scrollPositions.get(path) || 0,
  );
});
watch(() => pageHeader.title, (title) => {
  document.title = title ? `${title} · PaperSeek` : "PaperSeek";
});
</script>

<template>
  <a class="skip-link" href="#main-content">{{ u("skip") }}</a>
  <div class="app" :class="{ 'sb-collapsed': !small && !open }">
    <Sidebar v-if="!small" :key="historyKey" :visible="open" :history-enabled="historyEnabled">
      <template v-if="$slots.account" #account><slot name="account" :dismiss="closeSheet" /></template>
      <template v-if="$slots['history-off']" #history-off="scope"><slot name="history-off" v-bind="scope" /></template>
    </Sidebar>
    <div v-if="small && open" class="sb-sheet" role="dialog" aria-modal="true" :aria-label="u('nav')" @click.self="closeSheet" @keydown="sheetKey">
      <Sidebar :key="historyKey" visible :history-enabled="historyEnabled" @navigate="closeSheet">
        <template v-if="$slots.account" #account><slot name="account" :dismiss="closeSheet" /></template>
      <template v-if="$slots['history-off']" #history-off="scope"><slot name="history-off" v-bind="scope" /></template>
      </Sidebar>
    </div>
    <div class="inset">
      <header class="hdr">
        <button ref="toggle" class="btn btn-ghost btn-icon trigger" type="button" :aria-label="u(open ? 'hideSidebar' : 'showSidebar')" :aria-expanded="open" @click="toggleSidebar"><Icon name="panel-left" /></button>
        <span class="vsep"></span>
        <h1 :title="pageHeader.title">
          <template v-if="pageHeader.crumb"><RouterLink class="crumb" :to="pageHeader.crumb.to">{{ pageHeader.crumb.label }}</RouterLink><span class="sep-c">/</span></template>{{ pageHeader.title }}
        </h1>
        <div class="tools">
          <slot name="tools" />
          <a class="btn btn-ghost btn-icon" href="https://github.com/paperseek/paperseek" target="_blank" rel="noopener noreferrer" :aria-label="u('githubRepository')" :title="u('githubRepository')"><Icon name="github" /></a>
          <UiMenu :label="u('language')" trigger-class="btn btn-ghost btn-sm" align="end" :min-width="160">
            <template #trigger><Icon name="languages" />{{ language === "zh" ? "中文" : "English" }}</template>
            <button v-for="option in ['zh', 'en'] as const" :key="option" class="opt" role="menuitemradio" data-menu-item :aria-checked="language === option" @click="setLanguage(option)">
              {{ option === "zh" ? "简体中文" : "English" }}<Icon v-if="language === option" name="check" class="check" />
            </button>
          </UiMenu>
        </div>
      </header>
      <main id="main-content" ref="main" class="inset-main" tabindex="-1" @wheel.passive="stopRestoring" @pointerdown="stopRestoring" @keydown="stopRestoring">
        <div ref="content" class="stack" style="flex: 1 0 auto; min-width: 0"><slot /></div>
      </main>
    </div>
  </div>
  <Toaster />
</template>
