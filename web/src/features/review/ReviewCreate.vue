<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRefs, watch, watchEffect } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, ApiError, type JournalFilter } from "../../api/client";
import type { HistoryItem } from "../../api/types";
import AppDialog from "../../components/AppDialog.vue";
import Icon from "../../components/Icon.vue";
import UiSelect, { type SelectOption } from "../../components/UiSelect.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { venueBadges } from "../results/venue-labels";
import { connectionById, connectionOptions, loadSettings, workspaceSettings } from "../settings/store";
import SourceRunPicker from "../workspace/SourceRunPicker.vue";
import { invalidateHistory, listPaths, setHeader } from "../workspace/state";
import { MAX_REVIEW_PAPERS, reviewApi, type JobRequest } from "./api";
import { abstract, cancelReviewSelection, initializeReviewDraft, resetReviewDraft, reviewDraft, selectReviewPapers, usable, type DraftPaper as Paper, type SizeChoice } from "./review-draft";

// A deployment may block starting, e.g. when its usage allowance is exhausted.
const props = withDefaults(defineProps<{ submissionEnabled?: boolean }>(), { submissionEnabled: true });
const route = useRoute();
const router = useRouter();
const { t, runMessage, language } = useLocale();
const u = useUi();
initializeReviewDraft(language.value);
const { step, source, papers, cursor, query, journal, busy, error, created, loadedFor, moreOpen, suggesting, reasons } = toRefs(reviewDraft);
const { form, selected } = reviewDraft;
const loading = ref(false);
const openAbstracts = ref(new Set<string>());
const discardOpen = ref(false);
let controller: AbortController | null = null;
let generation = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let alive = true;

watchEffect(() => setHeader(u("newReview"), { label: u("reviews"), to: listPaths.reviews }));
onMounted(async () => {
  const revision = reviewDraft.revision;
  await loadSettings();
  if (alive && revision === reviewDraft.revision && !form.connection) form.connection = workspaceSettings.defaults?.review_llm || "";
});
onBeforeUnmount(() => {
  alive = false;
  generation++;
  controller?.abort();
  clearTimeout(timer);
  if (created.value && !busy.value) resetReviewDraft();
});

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, Math.round(value || low)));
const sectionCount = computed(() => (form.preset === "custom" ? clamp(form.custom, 1, 14) : { short: 1, standard: 3, detailed: 6 }[form.preset]));
const lengthPreset = computed(() => (sectionCount.value <= 1 ? "short" : sectionCount.value <= 3 ? "standard" : "detailed") as "short" | "standard" | "detailed");
// Paragraph length is counted in the output language's unit: characters for Chinese, words for English.
const sizeBounds = computed(() => (form.language === "zh" ? { min: 100, max: 1500, brief: 200, rich: 450 } : { min: 50, max: 800, brief: 120, rich: 250 }));
const paragraphCount = computed(() => (form.paragraphs === "auto" ? null : form.paragraphs === "custom" ? clamp(form.paragraphsCustom, 1, 10) : { brief: 3, rich: 5 }[form.paragraphs]));
const paragraphLength = computed(() => {
  const bounds = sizeBounds.value;
  return form.size === "auto" ? null : form.size === "custom" ? clamp(form.sizeCustom, bounds.min, bounds.max) : bounds[form.size];
});
watch(() => form.language, (next, previous) => {
  // Keep a custom length comparable when the unit switches between characters and words.
  if (next !== previous) form.sizeCustom = clamp(next === "en" ? form.sizeCustom / 2 : form.sizeCustom * 2, sizeBounds.value.min, sizeBounds.value.max);
}, { flush: "sync" });
const moreChanged = computed(() => Number(form.paragraphs !== "auto") + Number(form.size !== "auto"));
const paragraphOptions = computed<SelectOption<SizeChoice>[]>(() => [
  { value: "auto", label: u("len_auto"), hint: u("recommended"), desc: u("autoParagraphs", { range: { short: "2–3", standard: "3–5", detailed: "5–7" }[lengthPreset.value] }) },
  { value: "brief", label: u("len_brief"), desc: u("paragraphsEach", { n: 3 }) },
  { value: "rich", label: u("len_rich"), desc: u("paragraphsEach", { n: 5 }) },
  { value: "custom", label: u("len_custom"), desc: `1–10 ${u("paragraphUnit")}` },
]);
const sizeUnit = computed(() => u(form.language === "zh" ? "charUnit" : "wordUnit"));
const sizeOptions = computed<SelectOption<SizeChoice>[]>(() => {
  const bounds = sizeBounds.value;
  const size = (n: number) => u(form.language === "zh" ? "sizeChars" : "sizeWords", { n });
  return [
    { value: "auto", label: u("len_auto"), hint: u("recommended"), desc: u("autoSize") },
    { value: "brief", label: u("len_brief"), desc: size(bounds.brief) },
    { value: "rich", label: u("len_rich"), desc: size(bounds.rich) },
    { value: "custom", label: u("len_custom"), desc: `${bounds.min}–${bounds.max} ${sizeUnit.value}` },
  ];
});
const canContinue = computed(() => selected.size >= 2 && selected.size <= MAX_REVIEW_PAPERS && !loading.value && !suggesting.value);
const journalOptions = computed<SelectOption<string>[]>(() => [
  { value: "all", label: u("journalAll") },
  ...(["cas1", "cas2", "castop", "jcr1", "jcr2", "ccfa", "ccfb", "ccfc", "ranked"] as JournalFilter[]).map((value) => ({ value, label: u(`journal_${value}`) })),
]);
// Keep selected records from later pages visible when returning to this draft.
const visiblePapers = computed(() => query.value.trim() || journal.value !== "all" ? papers.value : [
  ...papers.value, ...[...selected.values()].filter(paper => !papers.value.some(item => item.uid === paper.uid)),
]);
// Papers a review of this size can use well: about 2–4 per planned paragraph, within the corpus limit.
const recommended = computed(() => {
  if (form.preset !== "custom" && !paragraphCount.value) return { short: [8, 20], standard: [20, 45], detailed: [40, 80] }[form.preset];
  const paragraphs = sectionCount.value * (paragraphCount.value ?? { short: 3, standard: 4, detailed: 6 }[lengthPreset.value]);
  return [clamp(paragraphs * 2, 2, MAX_REVIEW_PAPERS), clamp(paragraphs * 4, 2, MAX_REVIEW_PAPERS)];
});
const connectionOk = computed(() => Boolean(connectionById(form.connection)));
watch(() => workspaceSettings.connections, () => {
  if (workspaceSettings.loaded && !connectionById(form.connection)) form.connection = workspaceSettings.defaults?.review_llm || "";
});
function message(code: string) {
  return t(code) === code ? runMessage(code, "error") : t(code);
}
function report(e: unknown) {
  error.value = e instanceof ApiError ? e.code : "reviewError";
}

async function choose(id: string, restore: string[] = []) {
  cancelReviewSelection();
  const revision = ++reviewDraft.revision;
  const own = ++generation;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  loadedFor.value = "";
  error.value = "";
  papers.value = [];
  selected.clear();
  cursor.value = null;
  query.value = "";
  journal.value = "all";
  try {
    const detail = await api.run(id, controller.signal);
    if (own !== generation || revision !== reviewDraft.revision) return;
    if (!["completed", "completed_with_warnings"].includes(detail.status)) throw new ApiError(422, "review_source_not_completed");
    source.value = { id, title: detail.display_title || detail.question, count: detail.result_count ?? 0 };
    form.topic = detail.question;
    reasons.value = new Map();
    step.value = 2;
    let next: string | undefined;
    do {
      const page = await api.resultPage(id, { cursor: next }, controller.signal);
      if (own !== generation || revision !== reviewDraft.revision) return;
      papers.value.push(...usable(page.results));
      for (const paper of usable(page.results)) if (restore.includes(paper.uid) && abstract(paper)) selected.set(paper.uid, paper);
      cursor.value = page.next_cursor ?? null;
      next = cursor.value || undefined;
    } while (restore.length && selected.size < restore.length && next);
    loadedFor.value = id;
    if (route.query.source === id) await router.replace("/reviews/new");
  } catch (e) {
    if (own === generation && revision === reviewDraft.revision && !controller.signal.aborted) report(e);
  } finally {
    if (own === generation) loading.value = false;
  }
}
async function loadPapers(more = false) {
  if (!source.value) return;
  const revision = reviewDraft.revision;
  const own = ++generation;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = "";
  try {
    const page = await api.resultPage(source.value.id, {
      query: query.value.trim(), journal: journal.value === "all" ? "" : (journal.value as JournalFilter),
      cursor: more ? cursor.value || undefined : undefined,
    }, controller.signal);
    if (own !== generation || revision !== reviewDraft.revision) return;
    papers.value = more ? [...papers.value, ...usable(page.results)] : usable(page.results);
    cursor.value = page.next_cursor ?? null;
  } catch (e) {
    if (own === generation && revision === reviewDraft.revision && !controller.signal.aborted) report(e);
  } finally {
    if (own === generation) loading.value = false;
  }
}
watch([query, journal], () => {
  if (!source.value || step.value !== 3) return;
  clearTimeout(timer);
  timer = setTimeout(() => void loadPapers(), 200);
});
function pickSearch(id: string, item: HistoryItem) {
  if (source.value?.id !== id) cancelReviewSelection();
  source.value = { id, title: item.display_title || item.question, count: item.result_count };
}
function toggle(paper: Paper) {
  if (selected.has(paper.uid)) selected.delete(paper.uid);
  else if (selected.size < MAX_REVIEW_PAPERS) selected.set(paper.uid, paper);
}
function selectWithAbstract() {
  for (const paper of papers.value) if (abstract(paper) && selected.size < MAX_REVIEW_PAPERS) selected.set(paper.uid, paper);
}
function toggleAbstract(uid: string) {
  const next = new Set(openAbstracts.value);
  if (next.has(uid)) next.delete(uid);
  else next.add(uid);
  openAbstracts.value = next;
}
async function next() {
  if (step.value === 1 && source.value) {
    if (loadedFor.value !== source.value.id) await choose(source.value.id);
    else step.value = 2;
  } else if (step.value === 2 && form.topic.trim()) step.value = 3;
}
async function autoSelect() {
  if (!source.value || suggesting.value || !form.topic.trim() || !connectionOk.value) return;
  const [low, high] = recommended.value;
  await selectReviewPapers(Math.round((low + high) / 2));
}
async function discard() {
  generation++;
  controller?.abort();
  clearTimeout(timer);
  loading.value = false;
  resetReviewDraft();
  initializeReviewDraft(language.value);
  form.connection = workspaceSettings.defaults?.review_llm || "";
  discardOpen.value = false;
  await router.replace("/reviews/new");
}
async function prepare() {
  if (!props.submissionEnabled || busy.value || !source.value || !canContinue.value || !form.topic.trim() || !connectionOk.value) return;
  const revision = reviewDraft.revision;
  busy.value = true;
  error.value = "";
  try {
    if (!created.value) {
      const data = {
        topic: form.topic.trim(), language: form.language, source_run_id: source.value.id, paper_uids: [...selected.keys()],
        length: lengthPreset.value,
        section_count: sectionCount.value,
        ...(paragraphCount.value ? { paragraph_count: paragraphCount.value } : {}),
        ...(paragraphLength.value ? { paragraph_length: paragraphLength.value } : {}),
      };
      const signature = JSON.stringify(data);
      if (signature !== reviewDraft.createSignature) {
        reviewDraft.createSignature = signature;
        reviewDraft.createKey = crypto.randomUUID();
      }
      const result = await reviewApi.create(data, reviewDraft.createKey);
      if (revision !== reviewDraft.revision) return;
      created.value = result;
      invalidateHistory("reviews");
    }
    if (!alive) return;
    const job: JobRequest = { action: "prepare_outline", expected_version: created.value.version, connection_id: form.connection };
    const signature = JSON.stringify(job);
    if (signature !== reviewDraft.jobSignature) {
      reviewDraft.jobSignature = signature;
      reviewDraft.jobKey = crypto.randomUUID();
    }
    const id = created.value.id;
    await reviewApi.submit(id, job, reviewDraft.jobKey);
    if (revision !== reviewDraft.revision) return;
    resetReviewDraft();
    invalidateHistory("reviews");
    if (alive) await router.replace({ path: `/reviews/${id}`, query: { tab: "outline" } });
  } catch (e) {
    if (revision === reviewDraft.revision) report(e);
  } finally {
    if (revision === reviewDraft.revision) {
      busy.value = false;
      // Once saved, the review belongs to server history rather than the temporary workspace.
      if (!alive && created.value) resetReviewDraft();
    }
  }
}

const initialPapers = [route.query.paper].flat().filter((uid): uid is string => typeof uid === "string");
if (typeof route.query.source === "string" && !busy.value) {
  if (source.value && (source.value.id !== route.query.source || initialPapers.length)) {
    resetReviewDraft();
    initializeReviewDraft(language.value);
  }
  if (loadedFor.value !== route.query.source || initialPapers.length) void choose(route.query.source, initialPapers);
} else if (source.value && !loadedFor.value && !busy.value) void choose(source.value.id);
</script>

<template>
  <main class="page create">
    <form class="card create-card" aria-labelledby="new-review-title" @submit.prevent="step === 3 ? prepare() : next()">
      <div class="ch">
        <div><h2 id="new-review-title">{{ u("newReview") }}</h2><p>{{ u(step === 1 ? "newReviewStep1Desc" : step === 2 ? "newReviewStep2Desc" : "newReviewStep3Desc") }}</p></div>
        <ol class="steps-mini" :aria-label="u('steps')">
          <template v-for="(label, index) in ['stepSearch', 'stepSetup', 'stepPapers'] as const" :key="label">
            <li v-if="index" class="ln" aria-hidden="true"></li>
            <li class="row" :class="{ on: step === index + 1 }" style="gap: 6px" :aria-current="step === index + 1 ? 'step' : undefined">
              <span v-if="step > index + 1" class="step-dot done"><Icon name="check" size="xs" /></span>
              <span v-else class="step-dot" :class="{ active: step === index + 1 }">{{ index + 1 }}</span>{{ u(label) }}
            </li>
          </template>
        </ol>
      </div>

      <div v-if="source && !created" class="row" style="justify-content: space-between; gap: 12px; margin-bottom: 16px">
        <p class="help">{{ u("reviewDraftHint") }}</p>
        <button class="btn btn-ghost btn-sm" type="button" :disabled="busy" @click="discardOpen = true">{{ u("discardReviewDraft") }}</button>
      </div>

      <div v-if="step === 1" class="cc">
        <div class="field">
          <span class="label">{{ u("sourceSearch") }}</span>
          <SourceRunPicker :minimum="2" :selected="source?.id" :label="u('sourceSearch')" @select="pickSearch" />
          <p class="help">{{ u("reviewSourceRule") }}<RouterLink class="link" to="/search">{{ u("goSearch") }}</RouterLink></p>
        </div>
      </div>

      <div v-else-if="step === 2" class="cc">
        <div class="row" style="justify-content: space-between; gap: 12px; padding: 10px 12px; border-radius: 10px; background: var(--muted); flex-wrap: nowrap">
          <span class="row sm" style="gap: 8px; min-width: 0; flex-wrap: nowrap"><Icon name="text-search" class="muted" /><span class="truncate medium">{{ source?.title }}</span><span class="muted" style="white-space: nowrap">· {{ u("papersShort", { n: source?.count ?? 0 }) }}</span></span>
          <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="busy || suggesting || !!created" @click="step = 1">{{ u("change") }}</button>
        </div>
        <fieldset class="stack" style="gap: 24px; border: 0; padding: 0; margin: 0; min-width: 0" :disabled="busy || suggesting || !!created">
          <div class="field">
            <label class="label" for="review-topic">{{ u("topic") }}</label>
            <textarea id="review-topic" v-model="form.topic" class="input textarea" rows="3" maxlength="2000" required :placeholder="u('topicPh')"></textarea>
          </div>
          <div style="display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))">
            <div class="field">
              <span class="label">{{ u("outputLanguage") }}</span>
              <UiSelect v-model="form.language" :options="[{ value: 'zh', label: '中文' }, { value: 'en', label: 'English' }]" :label="u('outputLanguage')" />
            </div>
            <div class="field">
              <span class="label">{{ u("writingModel") }}</span>
              <UiSelect v-model="form.connection" :options="connectionOptions('llm')" :label="u('writingModel')" :placeholder="u('needsConnection')">
                <template #footer><div class="opt-sep"></div><RouterLink class="opt" to="/settings/models"><Icon name="plus" />{{ u("addConnection") }}</RouterLink></template>
              </UiSelect>
            </div>
          </div>
          <div class="field">
            <span id="length-label" class="label" style="margin-bottom: 0">{{ u("length") }}</span>
            <div class="choices" role="radiogroup" aria-labelledby="length-label" style="grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px">
              <label v-for="preset in ['short', 'standard', 'detailed'] as const" :key="preset" class="choice">
                <input v-model="form.preset" type="radio" name="review-length" :value="preset" /><span class="dot"></span>
                <span><span class="choice-title">{{ u(`len_${preset}`) }}</span><span class="choice-desc">{{ u("chapters", { n: { short: 1, standard: 3, detailed: 6 }[preset] }) }} · {{ u("papersRange", { range: { short: "8–20", standard: "20–45", detailed: "40–80" }[preset] }) }}</span></span>
              </label>
              <label class="choice">
                <input v-model="form.preset" type="radio" name="review-length" value="custom" /><span class="dot"></span>
                <span class="stack" style="gap: 2px"><span class="choice-title">{{ u("len_custom") }}</span><span class="row" style="gap: 6px; flex-wrap: nowrap"><input v-model.number="form.custom" class="input" type="number" min="1" max="14" :aria-label="u('sectionCount')" style="width: 56px; height: 24px; padding: 0 6px; font-size: 13px" @focus="form.preset = 'custom'" /><span class="choice-desc" style="margin: 0">{{ u("chapterUnit") }}</span></span></span>
              </label>
            </div>
            <p class="help">{{ u("lengthHelp") }}</p>
          </div>
          <details :open="moreOpen" style="border: 1px solid var(--border); border-radius: 10px" @toggle="moreOpen = ($event.target as HTMLDetailsElement).open">
            <summary class="row" style="cursor: pointer; padding: 10px 14px; justify-content: space-between; flex-wrap: nowrap">
              <span class="sm medium">{{ u("moreOptions") }}</span>
              <span class="row xs muted" style="gap: 6px; flex-wrap: nowrap"><template v-if="moreChanged">{{ u("changedCount", { n: moreChanged }) }}</template><Icon name="chevron-down" :style="{ transform: moreOpen ? 'rotate(180deg)' : undefined }" /></span>
            </summary>
            <div style="display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); padding: 4px 14px 14px">
              <div class="field">
                <span class="label">{{ u("paragraphsLabel") }}</span>
                <UiSelect v-model="form.paragraphs" :options="paragraphOptions" :label="u('paragraphsLabel')" />
                <span v-if="form.paragraphs === 'custom'" class="row" style="gap: 8px; flex-wrap: nowrap"><input v-model.number="form.paragraphsCustom" class="input" type="number" min="1" max="10" :aria-label="u('paragraphsLabel')" style="width: 88px" /><span class="sm muted">{{ u("paragraphUnit") }}</span></span>
              </div>
              <div class="field">
                <span class="label">{{ u("paragraphLength") }}</span>
                <UiSelect v-model="form.size" :options="sizeOptions" :label="u('paragraphLength')" />
                <span v-if="form.size === 'custom'" class="row" style="gap: 8px; flex-wrap: nowrap"><input v-model.number="form.sizeCustom" class="input" type="number" :min="sizeBounds.min" :max="sizeBounds.max" :aria-label="u('paragraphLength')" style="width: 88px" /><span class="sm muted">{{ sizeUnit }}</span></span>
                <p class="help">{{ u("sizeHelp") }}</p>
              </div>
            </div>
          </details>
          <slot name="service" />
        </fieldset>
      </div>

      <div v-else class="cc">
        <div class="row" style="justify-content: space-between; gap: 12px; padding: 10px 12px; border-radius: 10px; background: var(--muted); flex-wrap: nowrap">
          <span class="row sm" style="gap: 8px; min-width: 0; flex-wrap: nowrap"><Icon name="file" class="muted" /><span class="medium" style="white-space: nowrap">{{ u(`len_${form.preset}`) }} · {{ u("chapters", { n: sectionCount }) }}</span><span class="muted truncate">· {{ form.topic }}</span></span>
          <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="busy || suggesting || !!created" @click="step = 2">{{ u("adjust") }}</button>
        </div>
        <div class="row" style="justify-content: space-between; gap: 12px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px">
          <span class="stack" style="gap: 2px; min-width: 0; flex: 1 1 240px"><span class="sm medium">{{ u("recommendedPapers", { range: `${recommended[0]}–${recommended[1]}` }) }}</span><span class="xs muted">{{ u("autoSelectHint") }}</span></span>
          <button class="btn btn-outline btn-sm" type="button" style="height: 28px; white-space: nowrap" :disabled="suggesting || busy || loading || !!created || !connectionOk" :title="connectionOk ? undefined : u('needLlmToStart')" @click="autoSelect"><Icon :name="suggesting ? 'loader' : 'sparkles'" />{{ suggesting ? u("autoSelecting") : u("autoSelect") }}</button>
          <button v-if="suggesting" class="btn btn-ghost btn-sm" type="button" @click="cancelReviewSelection">{{ u("cancelSelection") }}</button>
        </div>
        <div class="field">
          <div class="row" style="justify-content: space-between"><span class="label">{{ u("papers") }}</span><span class="row" style="gap: 2px"><button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="loading || suggesting || busy || !!created" @click="selectWithAbstract">{{ u("selectWithAbstract") }}</button><button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="!selected.size || suggesting || busy || !!created" @click="selected.clear()">{{ u("clearSelection") }}</button></span></div>
          <div class="row" style="gap: 8px; flex-wrap: nowrap">
            <div class="ig grow"><Icon name="search" /><input v-model="query" type="search" :placeholder="u('searchPapers')" :aria-label="u('searchPapers')" /></div>
            <UiSelect v-model="journal" :options="journalOptions" :label="u('journalFilter')" :prefix="u('journalFilter')" size="sm" chevron="down" />
          </div>
          <div class="pick" style="max-height: 360px; overflow-y: auto; scrollbar-gutter: stable" :aria-busy="loading">
            <div v-for="paper in visiblePapers" :key="paper.uid" class="paper-pick" :class="{ off: !abstract(paper) }">
              <label v-if="abstract(paper)" class="cb" style="margin-top: 2px" :aria-label="u('selectPaper')"><input type="checkbox" :checked="selected.has(paper.uid)" :disabled="busy || suggesting || !!created || (!selected.has(paper.uid) && selected.size >= MAX_REVIEW_PAPERS)" @change="toggle(paper)" /><span class="box"><Icon name="check" /></span></label>
              <label v-else class="cb" style="margin-top: 2px; opacity: .5" :aria-label="u('noAbstractCannotSelect')"><input type="checkbox" disabled /><span class="box"></span></label>
              <div class="stack" style="min-width: 0"><span class="t sm medium">{{ paper.title }}</span><span class="xs muted">{{ [paper.authors?.slice(0, 3).join(", "), paper.year, paper.score != null ? `${paper.score}/10` : u("unscored")].filter(Boolean).join(" · ") }}</span><span v-if="venueBadges(u, paper.journal_metrics).length || paper.acceptance?.label" class="row" style="gap: 4px; margin-top: 2px"><span v-if="paper.acceptance?.label" class="badge badge-outline">{{ paper.acceptance.label }}</span><span v-for="badge in venueBadges(u, paper.journal_metrics)" :key="badge" class="badge badge-outline">{{ badge }}</span></span><span v-if="selected.has(paper.uid) && reasons.get(paper.uid)" class="xs muted">{{ reasons.get(paper.uid) }}</span></div>
              <button v-if="abstract(paper)" class="btn btn-ghost btn-sm" type="button" style="height: 24px; padding: 0 6px" :aria-expanded="openAbstracts.has(paper.uid)" @click="toggleAbstract(paper.uid)">{{ u("abstract") }}</button>
              <span v-else class="xs muted" style="white-space: nowrap">{{ u("noAbstractShort") }}</span>
              <p v-if="openAbstracts.has(paper.uid)" class="abs">{{ abstract(paper) }}</p>
            </div>
            <div v-if="loading && !visiblePapers.length" class="paper-pick"><span></span><span class="skeleton" style="height: 14px"></span><span></span></div>
            <p v-else-if="!visiblePapers.length" class="sm muted" style="padding: 12px">{{ u("noMatches") }}</p>
            <button v-if="cursor" class="pick-row" type="button" style="justify-content: center; border-top: 1px solid var(--border)" :disabled="loading" @click="loadPapers(true)"><Icon v-if="loading" name="loader" size="sm" /><span class="sm muted">{{ u("loadMore") }}</span></button>
          </div>
        </div>
        <p v-if="created && error" class="help">{{ u("draftSaved") }} <RouterLink class="link" :to="`/reviews/${created.id}`">{{ u("openReview") }}</RouterLink></p>
      </div>

      <div v-if="error" class="alert" role="alert" style="grid-template-columns: 16px 1fr; margin-bottom: 16px"><Icon name="fail" /><p class="alert-title">{{ message(error) }}</p></div>

      <div class="cf">
        <RouterLink v-if="step === 1" class="btn btn-ghost btn-sm" :to="listPaths.reviews">{{ u("cancel") }}</RouterLink>
        <button v-else class="btn btn-ghost btn-sm" type="button" :disabled="busy || suggesting || !!created" @click="step = (step - 1) as 1 | 2">{{ u("previous") }}</button>
        <div class="row" style="gap: 12px">
          <span v-if="step === 3" class="sm muted" aria-live="polite"><span class="medium" style="color: var(--foreground)">{{ u("selectedOf", { n: selected.size }) }}</span> · {{ u("atLeastTwo") }}</span>
          <button v-if="step < 3" class="btn btn-sm" type="submit" :disabled="step === 1 ? !source || loading : !form.topic.trim()"><Icon v-if="loading && step === 1" name="loader" />{{ u("next") }}</button>
          <template v-else>
            <slot name="cost" />
            <button class="btn btn-sm" type="submit" :disabled="!submissionEnabled || busy || !canContinue || !form.topic.trim() || !connectionOk" :title="connectionOk ? undefined : u('needLlmToStart')"><Icon v-if="busy" name="loader" />{{ busy ? u("starting") : u("generateOutline") }}</button>
          </template>
        </div>
      </div>
    </form>
    <AppDialog v-if="discardOpen" :title="u('discardReviewDraft')" :description="u('discardReviewDraftHint')" @close="discardOpen = false">
      <template #footer>
        <button class="btn btn-outline btn-sm" type="button" @click="discardOpen = false">{{ u("cancel") }}</button>
        <button class="btn btn-sm" type="button" @click="discard">{{ u("discardReviewDraft") }}</button>
      </template>
    </AppDialog>
  </main>
</template>
