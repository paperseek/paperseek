<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import Icon from "../../components/Icon.vue";
import UiMenu from "../../components/UiMenu.vue";
import UiSelect from "../../components/UiSelect.vue";
import UnsavedChangesDialog from "../../components/UnsavedChangesDialog.vue";
import { toast } from "../../components/toast";
import { useUnsavedChanges } from "../../components/use-unsaved-changes";
import { useUi } from "../../locales/ui";
import { reviewApi, type Outline, type Review } from "./api";

const props = defineProps<{ review: Review; disabled: boolean }>();
const emit = defineEmits<{ confirm: [save?: () => Promise<unknown>]; save: [operation: () => Promise<unknown>]; dirty: [value: boolean] }>();
const u = useUi();
const draft = ref<Outline>({ sections: [] });
const original = ref("");
const current = ref(0);
const form = ref<HTMLFormElement>();
let version = 0;
let revision = 0;
const dirty = computed(() => JSON.stringify(draft.value) !== original.value);
const { pending, decide } = useUnsavedChanges(dirty);
const section = computed(() => draft.value.sections[current.value]);

watch(() => props.review.version, () => {
  if (version && dirty.value) return;
  draft.value = JSON.parse(JSON.stringify(props.review.content.outline || { sections: [] }));
  original.value = JSON.stringify(draft.value);
  version = props.review.version;
  revision = props.review.content.outline_revision || 0;
  current.value = Math.min(current.value, Math.max(0, draft.value.sections.length - 1));
}, { immediate: true });
watch(dirty, (value) => emit("dirty", value), { immediate: true });

const available = computed(() => new Set(props.review.content.cards
  ?.filter((card) => card.evidence.length && props.review.content.evidence_errors?.[card.paper_id] !== "review_evidence_unusable")
  .map((card) => card.paper_id)));
const papers = computed(() => props.review.content.papers);
function label(id: string) {
  const index = papers.value.findIndex((paper) => paper.id === id);
  const paper = papers.value[index];
  if (!paper) return id;
  // "Chen L" / "A. Researcher" / "Researcher, A." → the first token that is not an initial.
  const author = paper.authors?.[0] || "";
  const tokens = (author.includes(",") ? author.split(",")[0] : author).split(/\s+/).filter(Boolean);
  const surname = tokens.find((token) => token.length > 2 && !token.endsWith(".")) || tokens[0] || paper.title.slice(0, 12);
  return `[${index + 1}] ${surname}${paper.year ? ` ${paper.year}` : ""}`;
}
// Papers no section plans keep a reason: the planner's own, or the user's removal.
const leftOut = computed(() => {
  const planned = new Set(draft.value.sections.flatMap((item) => item.paper_ids));
  const reasons = new Map((draft.value.excluded || []).map((item) => [item.paper_id, item.reason]));
  return papers.value.filter((paper) => available.value.has(paper.id) && !planned.has(paper.id))
    .map((paper) => ({ paper_id: paper.id, reason: reasons.get(paper.id) || u("removedByUser") }));
});
const kindOptions = computed(() => (["theme", "overview", "conclusion"] as const).map((value) => ({ value, label: u(`kind_${value}`) })));

function add() {
  draft.value.sections.push({ id: `s_${crypto.randomUUID()}`, title: "", kind: "theme", parent_id: "", goal: "", comparisons: [], paper_ids: [], paragraphs: [{ goal: "", paper_ids: [] }] });
  current.value = draft.value.sections.length - 1;
}
function move<T>(items: T[], index: number, delta: number) {
  const next = index + delta;
  if (next < 0 || next >= items.length) return false;
  [items[index], items[next]] = [items[next], items[index]];
  return true;
}
function moveSection(delta: number) {
  if (move(draft.value.sections, current.value, delta)) current.value += delta;
}
function removeSection() {
  const id = section.value?.id;
  draft.value.sections = draft.value.sections.filter((item) => item.id !== id);
  for (const item of draft.value.sections) if (item.parent_id === id) item.parent_id = "";
  current.value = Math.max(0, current.value - 1);
}
function removePaper(paperId: string) {
  if (!section.value) return;
  section.value.paper_ids = section.value.paper_ids.filter((id) => id !== paperId);
  for (const paragraph of section.value.paragraphs || []) paragraph.paper_ids = paragraph.paper_ids.filter((id) => id !== paperId);
}
function addParagraph() {
  if (section.value) (section.value.paragraphs ||= []).push({ goal: "", paper_ids: [...section.value.paper_ids] });
}
async function saveOutline() {
  draft.value.excluded = leftOut.value;
  const outline = JSON.parse(JSON.stringify(draft.value));
  const value = await reviewApi.outline(props.review.id, { expected_version: version, expected_revision: revision, outline });
  original.value = JSON.stringify(outline);
  version = value.version;
  revision = value.content.outline_revision || 0;
  toast(u("savedToast"), { tone: "ok" });
}
async function save() {
  if (props.disabled || !dirty.value) return;
  // Only the open section is rendered; jump to the first incomplete one first.
  const incomplete = draft.value.sections.findIndex((item) => !item.title.trim() || !item.goal.trim() || (item.paragraphs || []).some((paragraph) => !paragraph.goal.trim()));
  if (incomplete >= 0 && incomplete !== current.value) {
    current.value = incomplete;
    await nextTick();
  }
  if (!form.value?.reportValidity()) return;
  emit("save", saveOutline);
}
function discard() {
  draft.value = JSON.parse(original.value);
  current.value = Math.min(current.value, Math.max(0, draft.value.sections.length - 1));
}
</script>

<template>
  <form ref="form" class="stack" style="gap: 16px" @submit.prevent="save">
    <div class="field">
      <label class="label" for="review-focus">{{ u("focus") }}</label>
      <textarea id="review-focus" v-model="draft.focus" class="input textarea" rows="2" maxlength="2000" style="min-height: 56px" :disabled="disabled"></textarea>
    </div>
    <div style="display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start">
      <nav class="card flush" style="flex: 1 1 260px" :aria-label="u('sections')">
        <div class="row" style="justify-content: space-between; padding: 10px 12px 10px 16px; border-bottom: 1px solid var(--border)">
          <span class="sm medium">{{ u("sections") }} <span class="muted" style="font-weight: 400">· {{ draft.sections.length }}</span></span>
          <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="disabled || draft.sections.length >= 14" @click="add"><Icon name="plus" />{{ u("add") }}</button>
        </div>
        <ol class="list" style="padding: 6px">
          <li v-for="(item, index) in draft.sections" :key="item.id">
            <button class="list-row" :class="{ active: index === current }" type="button" :aria-current="index === current ? 'true' : undefined" @click="current = index">
              <Icon name="grip" class="grip" />
              <span class="xs muted num">{{ index + 1 }}</span>
              <span class="grow truncate sm" :class="{ medium: index === current, muted: !item.title }">{{ item.title || u("sectionTitle") }}</span>
              <span class="badge badge-outline muted">{{ u(`kind_${item.kind}` as "kind_theme") }}</span>
            </button>
          </li>
        </ol>
      </nav>

      <section v-if="section" class="card c" style="flex: 999 1 560px" aria-labelledby="section-editor-title">
        <div class="card-header">
          <div><h2 id="section-editor-title" class="card-title"><span class="muted" style="font-weight: 500">{{ current + 1 }} · </span>{{ section.title || u("sectionTitle") }}</h2></div>
          <div class="row" style="gap: 2px; margin: -4px -8px 0 0">
            <button class="btn btn-ghost btn-icon btn-xs" type="button" :aria-label="u('moveUp')" :disabled="disabled || current === 0" @click="moveSection(-1)"><Icon name="arrow-up" /></button>
            <button class="btn btn-ghost btn-icon btn-xs" type="button" :aria-label="u('moveDown')" :disabled="disabled || current === draft.sections.length - 1" @click="moveSection(1)"><Icon name="arrow-down" /></button>
            <button class="btn btn-ghost btn-icon btn-xs danger" type="button" :aria-label="u('removeSection')" :disabled="disabled || draft.sections.length === 1" @click="removeSection"><Icon name="trash" /></button>
          </div>
        </div>
        <fieldset class="card-content stack" style="gap: 16px; border: 0; margin: 0; min-width: 0" :disabled="disabled">
          <div style="display: grid; gap: 12px; grid-template-columns: minmax(0, 2fr) minmax(140px, 1fr)">
            <div class="field"><label class="label" for="section-title">{{ u("sectionTitle") }}</label><input id="section-title" v-model="section.title" class="input" required maxlength="250" /></div>
            <div class="field"><span class="label">{{ u("sectionKind") }}</span><UiSelect v-model="section.kind!" :options="kindOptions" :label="u('sectionKind')" :disabled="disabled" /></div>
          </div>
          <div style="display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr))">
            <div class="field"><label class="label" for="section-goal">{{ u("goal") }}</label><textarea id="section-goal" v-model="section.goal" class="input textarea" rows="3" required maxlength="1500"></textarea></div>
            <div class="field">
              <label class="label" for="section-comparisons">{{ u("comparisons") }} <span class="muted" style="font-weight: 400">{{ u("perLine") }}</span></label>
              <textarea id="section-comparisons" class="input textarea" rows="3" :value="section.comparisons?.join('\n')" @input="section.comparisons = ($event.target as HTMLTextAreaElement).value.split('\n').filter(Boolean)"></textarea>
            </div>
          </div>
          <div class="field">
            <span class="label">{{ u("supporting") }} <span class="muted" style="font-weight: 400">{{ u("papersN", { n: section.paper_ids.length }) }}</span></span>
            <div class="chips">
              <span v-for="paperId in section.paper_ids" :key="paperId" class="chip" :title="papers.find((paper) => paper.id === paperId)?.title">{{ label(paperId) }}<button type="button" :aria-label="`${u('remove')} ${label(paperId)}`" :disabled="disabled" @click="removePaper(paperId)"><Icon name="x" size="xs" /></button></span>
              <UiMenu :label="u('addPaper')" trigger-class="chip" :min-width="320" :disabled="disabled" style="display: inline-flex">
                <template #trigger><span class="row" style="gap: 4px; padding-right: 4px"><Icon name="plus" size="xs" />{{ u("add") }}</span></template>
                <button v-for="paper in papers.filter((item) => available.has(item.id) && !section!.paper_ids.includes(item.id))" :key="paper.id" class="opt" role="menuitem" data-menu-item type="button" @click="section!.paper_ids.push(paper.id)">
                  <span class="xs muted num">{{ label(paper.id).split(" ")[0] }}</span><span class="truncate">{{ paper.title }}</span>
                </button>
              </UiMenu>
            </div>
          </div>
          <div class="sep"></div>
          <div class="stack" style="gap: 8px">
            <div class="row" style="justify-content: space-between">
              <span class="label">{{ u("paragraphPlan") }}</span>
              <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="disabled || (section.paragraphs?.length || 0) >= 12" @click="addParagraph"><Icon name="plus" />{{ u("addParagraph") }}</button>
            </div>
            <p class="help">{{ u("paragraphPlanHelp") }}</p>
            <div v-for="(paragraph, index) in section.paragraphs" :key="index" class="para">
              <div class="row" style="justify-content: space-between">
                <span class="xs medium muted">{{ u("paragraphN", { n: index + 1 }) }}</span>
                <span class="row" style="gap: 2px">
                  <button class="btn btn-ghost btn-icon btn-xs" type="button" style="width: 24px; height: 24px" :aria-label="u('moveUp')" :disabled="index === 0" @click="move(section.paragraphs!, index, -1)"><Icon name="arrow-up" size="sm" /></button>
                  <button class="btn btn-ghost btn-icon btn-xs" type="button" style="width: 24px; height: 24px" :aria-label="u('moveDown')" :disabled="index === (section.paragraphs?.length || 0) - 1" @click="move(section.paragraphs!, index, 1)"><Icon name="arrow-down" size="sm" /></button>
                  <button class="btn btn-ghost btn-icon btn-xs" type="button" style="width: 24px; height: 24px" :aria-label="u('removeParagraph', { n: index + 1 })" :disabled="section.paragraphs?.length === 1" @click="section.paragraphs?.splice(index, 1)"><Icon name="x" size="sm" /></button>
                </span>
              </div>
              <textarea v-model="paragraph.goal" class="input textarea" rows="2" required maxlength="1200" :aria-label="u('paragraphIdea', { n: index + 1 })" style="min-height: 52px"></textarea>
              <div class="chips">
                <span v-for="paperId in paragraph.paper_ids" :key="paperId" class="chip">{{ label(paperId) }}<button type="button" :aria-label="`${u('remove')} ${label(paperId)}`" @click="paragraph.paper_ids = paragraph.paper_ids.filter((id) => id !== paperId)"><Icon name="x" size="xs" /></button></span>
                <UiMenu v-if="section.paper_ids.some((id) => !paragraph.paper_ids.includes(id))" :label="u('addPaper')" trigger-class="chip" :min-width="320" style="display: inline-flex">
                  <template #trigger><span class="row" style="gap: 4px; padding-right: 4px"><Icon name="plus" size="xs" />{{ u("add") }}</span></template>
                  <button v-for="paperId in section.paper_ids.filter((id) => !paragraph.paper_ids.includes(id))" :key="paperId" class="opt" role="menuitem" data-menu-item type="button" @click="paragraph.paper_ids.push(paperId)">
                    <span class="xs muted num">{{ label(paperId).split(" ")[0] }}</span><span class="truncate">{{ papers.find((paper) => paper.id === paperId)?.title }}</span>
                  </button>
                </UiMenu>
              </div>
            </div>
          </div>
        </fieldset>
      </section>
    </div>
    <details v-if="leftOut.length" class="card c" style="padding: 12px 16px">
      <summary class="row sm medium" style="cursor: pointer; gap: 8px">{{ u("excludedPapers", { n: leftOut.length }) }}<Icon name="chevron-down" class="muted" /></summary>
      <p class="help" style="margin-top: 6px">{{ u("excludedHelp") }}</p>
      <ul class="stack" style="gap: 6px; margin-top: 8px; padding-left: 20px">
        <li v-for="item in leftOut" :key="item.paper_id" class="xs muted"><span class="medium" style="color: var(--foreground)">{{ label(item.paper_id) }} {{ papers.find((paper) => paper.id === item.paper_id)?.title }}</span>{{ u("colon") }}{{ item.reason }}</li>
      </ul>
    </details>
    <div v-if="dirty" class="bulk" role="region" :aria-label="u('unsavedOutline')">
      <span class="sm medium">{{ u("unsavedOutline") }}</span>
      <span class="div"></span>
      <button class="btn" type="button" :disabled="disabled" @click="discard">{{ u("discard") }}</button>
      <button class="btn light" type="submit" :disabled="disabled">{{ u("save") }}</button>
    </div>
  </form>
  <UnsavedChangesDialog :open="pending" @decide="decide" />
</template>
