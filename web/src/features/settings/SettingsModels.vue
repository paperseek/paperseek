<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "../../api/client";
import AppDialog from "../../components/AppDialog.vue";
import Icon from "../../components/Icon.vue";
import UiMenu from "../../components/UiMenu.vue";
import UiSelect from "../../components/UiSelect.vue";
import { toast } from "../../components/toast";
import { useLocale } from "../../locales";
import { useUi, useUiError } from "../../locales/ui";
import { useDates } from "../workspace/format";
import ConnectionSheet from "./ConnectionSheet.vue";
import { connectionOptions, connectionsOf, loadSettings, settingsApi, workspaceSettings, type Connection, type ConnectionKind, type Defaults } from "./store";

const route = useRoute();
const router = useRouter();
const { t, providerName } = useLocale();
const u = useUi();
const uiError = useUiError();
const { ago } = useDates();
const kinds: Array<{ value: ConnectionKind; label: string }> = [
  { value: "llm", label: "LLM" }, { value: "embedding", label: "Embedding" }, { value: "reranker", label: "Rerank" }, { value: "decision", label: "System One" },
];
const kind = ref<ConnectionKind>((kinds.some((item) => item.value === route.query.kind) ? route.query.kind : "llm") as ConnectionKind);
const sheet = ref<{ kind: ConnectionKind; connection?: Connection } | null>(null);
const testing = reactive(new Set<string>());
const removing = ref<Connection | null>(null);
const replacement = ref("");
const removeBusy = ref(false);
const removeError = ref("");
const rows = computed(() => connectionsOf(kind.value));
const defaults = computed(() => workspaceSettings.defaults);
watch(kind, (value) => void router.replace({ query: { ...route.query, kind: value === "llm" ? undefined : value } }));
watch(() => route.query.add, (value) => {
  if (typeof value === "string" && kinds.some((item) => item.value === value)) {
    kind.value = value as ConnectionKind;
    sheet.value = { kind: kind.value };
    void router.replace({ query: { ...route.query, add: undefined } });
  }
}, { immediate: true });

const preOptions = computed(() => (["local_fusion", "embedding_rerank", "decision"] as const).map((mode) => {
  const missing = mode === "embedding_rerank" ? !connectionsOf("embedding").length : mode === "decision" ? !connectionsOf("decision").length : false;
  return { value: mode, label: u(`pre_${mode}`), desc: missing ? u("needsConnection") : t(`preRanking_${mode}_help`), disabled: missing };
}));
async function saveDefault(patch: Partial<Defaults>) {
  try {
    workspaceSettings.defaults = await settingsApi.saveDefaults(patch);
    toast(u("savedToast"), { tone: "ok" });
  } catch (e) {
    toast(uiError(e instanceof ApiError ? e.code : "generic"), { tone: "fail" });
  }
}
const searchLlm = computed({ get: () => defaults.value?.search_llm || "", set: (value) => void saveDefault({ search_llm: value }) });
const reviewLlm = computed({ get: () => defaults.value?.review_llm || "", set: (value) => void saveDefault({ review_llm: value }) });
const preRanking = computed({ get: () => defaults.value?.pre_ranking || "local_fusion", set: (value) => void saveDefault({ pre_ranking: value }) });

function roles(item: Connection): string[] {
  const value = defaults.value;
  if (!value) return [];
  if (item.kind === "llm") return [value.search_llm === item.id ? u("useSearch") : "", value.review_llm === item.id ? u("useReview") : ""].filter(Boolean);
  return value[item.kind] === item.id ? [u("useDefault")] : [];
}
function initial(item: Connection) {
  return (item.model.replace(/^.*\//, "").match(/[a-z0-9]/i)?.[0] || "?").toUpperCase();
}
function apiLabel(item: Connection) {
  if (item.kind !== "llm") return item.kind === "decision" ? "SystemOne" : item.kind === "embedding" ? "Embeddings" : "Rerank";
  return { openai_chat: "OpenAI Chat Completions", openai_responses: "OpenAI Responses", anthropic_messages: "Anthropic Messages" }[item.api_type] || item.api_type;
}
async function test(item: Connection, reload = true) {
  testing.add(item.id);
  try {
    await settingsApi.test(item.id);
  } catch {
    /* The row shows the recorded failure after reload. */
  } finally {
    testing.delete(item.id);
    if (reload) await loadSettings(true);
  }
}
async function testAll() {
  // One reload after every test has recorded its result, so no row shows an earlier snapshot.
  await Promise.all(rows.value.map((item) => test(item, false)));
  await loadSettings(true);
}
async function setDefault(item: Connection, role: "search_llm" | "review_llm" | ConnectionKind) {
  await saveDefault({ [role === "llm" ? "search_llm" : role]: item.id });
}
function askRemove(item: Connection) {
  removing.value = item;
  replacement.value = "";
  removeError.value = "";
}
const inUse = computed(() => Boolean(removing.value && roles(removing.value).length));
const replacements = computed(() => connectionOptions(removing.value?.kind || "llm").filter((option) => option.value !== removing.value?.id));
async function remove() {
  if (!removing.value || removeBusy.value) return;
  removeBusy.value = true;
  removeError.value = "";
  try {
    await settingsApi.remove(removing.value.id, inUse.value ? replacement.value : "");
    removing.value = null;
    toast(u("deletedToast"), { tone: "ok" });
    await loadSettings(true);
  } catch (e) {
    removeError.value = e instanceof ApiError ? e.code : "generic";
  } finally {
    removeBusy.value = false;
  }
}
async function saved() {
  sheet.value = null;
  toast(u("savedToast"), { tone: "ok" });
  await loadSettings(true);
}
</script>

<template>
  <section class="card c" aria-labelledby="defaults-title">
    <div class="card-header">
      <div><h2 id="defaults-title" class="card-title">{{ u("defaultsTitle") }}</h2><p class="help" style="margin-top: 4px">{{ u("defaultsDesc") }}</p></div>
    </div>
    <div class="card-content" style="display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr))">
      <div class="field" style="gap: 6px"><span class="label" style="font-size: 12px">{{ u("defaultSearchModel") }}</span><UiSelect v-model="searchLlm" :options="connectionOptions('llm')" :label="u('defaultSearchModel')" :placeholder="u('needsConnection')" size="sm" /></div>
      <div class="field" style="gap: 6px"><span class="label" style="font-size: 12px">{{ u("defaultReviewModel") }}</span><UiSelect v-model="reviewLlm" :options="connectionOptions('llm')" :label="u('defaultReviewModel')" :placeholder="u('needsConnection')" size="sm" /></div>
      <div class="field" style="gap: 6px"><span class="label" style="font-size: 12px">{{ u("preRanking") }}</span><UiSelect v-model="preRanking" :options="preOptions" :label="u('preRanking')" size="sm" /></div>
    </div>
  </section>

  <div class="toolbar">
    <div class="seg" role="radiogroup" :aria-label="u('modelKinds')">
      <label v-for="item in kinds" :key="item.value"><input v-model="kind" type="radio" name="model-kind" :value="item.value" /><span>{{ item.label }} · {{ connectionsOf(item.value).length }}</span></label>
    </div>
    <div class="row end" style="gap: 8px">
      <button class="btn btn-outline btn-sm" type="button" :disabled="!rows.length || testing.size > 0" @click="testAll"><Icon v-if="testing.size" name="loader" />{{ u("testAll") }}</button>
      <button class="btn btn-sm" type="button" @click="sheet = { kind }"><Icon name="plus" />{{ u("addConnection") }}</button>
    </div>
  </div>

  <div v-if="workspaceSettings.loaded && !rows.length" class="empty">
    <span class="empty-media"><Icon name="sliders" size="lg" /></span>
    <p class="empty-title">{{ u("noConnections", { kind: kinds.find((item) => item.value === kind)?.label || "" }) }}</p>
    <p class="empty-desc">{{ u(`noConnectionsDesc_${kind}`) }}</p>
    <button class="btn btn-sm" type="button" style="margin-top: 8px" @click="sheet = { kind }"><Icon name="plus" />{{ u("addConnection") }}</button>
  </div>
  <div v-else class="table-wrap">
    <table class="table">
      <thead>
        <tr><th>{{ u("colConnection") }}</th><th>{{ u("colEndpoint") }}</th><th class="shrink">{{ u("colUse") }}</th><th class="shrink">{{ u("colConnectivity") }}</th><th class="shrink"><span class="sr-only">{{ u("colActions") }}</span></th></tr>
      </thead>
      <tbody v-if="!workspaceSettings.loaded">
        <tr v-for="index in 2" :key="index"><td style="min-width: 200px"><span class="skeleton" style="height: 28px; width: 70%"></span></td><td><span class="skeleton" style="height: 14px; width: 60%"></span></td><td></td><td></td><td></td></tr>
      </tbody>
      <tbody v-else>
        <tr v-for="item in rows" :key="item.id">
          <td style="min-width: 200px"><div class="row" style="gap: 10px; flex-wrap: nowrap"><span class="prov" style="font-size: 12px">{{ initial(item) }}</span><span class="stack" style="min-width: 0"><span class="medium truncate">{{ item.model }}</span><span class="xs muted">{{ item.provider === "typesafe" ? "TypeSafe (Jev)" : providerName(item.provider) }}</span></span></div></td>
          <td class="wrap" style="min-width: 180px"><span class="sm mono muted">{{ item.base_url.replace(/^https?:\/\//, "").split("/")[0] }}</span><p class="xs muted">{{ apiLabel(item) }}</p></td>
          <td><div v-if="roles(item).length" class="row" style="gap: 4px; flex-wrap: nowrap"><span v-for="role in roles(item)" :key="role" class="badge badge-secondary">{{ role }}</span></div><span v-else class="xs muted">—</span></td>
          <td>
            <span v-if="testing.has(item.id)" class="row sm muted" style="gap: 6px; flex-wrap: nowrap"><Icon name="loader" />{{ u("testing") }}</span>
            <div v-else-if="item.test.status !== 'untested'" class="stack" style="gap: 2px">
              <span class="row sm" style="gap: 6px; flex-wrap: nowrap" :style="item.test.status === 'failed' ? 'color: var(--destructive)' : undefined"><Icon :name="item.test.status === 'ok' ? 'ok' : 'fail'" />{{ u(item.test.status === "ok" ? "connOk" : "connFailed") }}</span>
              <span class="xs muted num" style="padding-left: 22px">{{ item.test.status === "ok" ? `${item.test.latency_ms ?? 0} ms` : uiError(item.test.code || '') }} · {{ ago(item.test.tested_at) }}</span>
            </div>
            <span v-else class="row sm muted" style="gap: 6px; flex-wrap: nowrap"><Icon name="circle-minus" />{{ u("untested") }}</span>
          </td>
          <td>
            <div class="row" style="gap: 2px; flex-wrap: nowrap">
              <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="testing.has(item.id)" @click="test(item)">{{ u("test") }}</button>
              <UiMenu :label="u('connMenu')" align="end" :min-width="176">
                <template #trigger><Icon name="ellipsis" /></template>
                <button class="opt" role="menuitem" data-menu-item type="button" @click="sheet = { kind: item.kind, connection: item }"><Icon name="pencil" />{{ u("edit") }}</button>
                <template v-if="item.kind === 'llm'">
                  <button class="opt" role="menuitem" data-menu-item type="button" :disabled="defaults?.search_llm === item.id" @click="setDefault(item, 'search_llm')"><Icon name="search" />{{ u("setSearchDefault") }}</button>
                  <button class="opt" role="menuitem" data-menu-item type="button" :disabled="defaults?.review_llm === item.id" @click="setDefault(item, 'review_llm')"><Icon name="file-text" />{{ u("setReviewDefault") }}</button>
                </template>
                <button v-else class="opt" role="menuitem" data-menu-item type="button" :disabled="defaults?.[item.kind] === item.id" @click="setDefault(item, item.kind)"><Icon name="check" />{{ u("setDefault") }}</button>
                <div class="opt-sep" role="separator"></div>
                <button class="opt danger" role="menuitem" data-menu-item type="button" @click="askRemove(item)"><Icon name="trash" />{{ u("remove") }}</button>
              </UiMenu>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <p class="xs muted row" style="gap: 6px"><Icon name="lock" size="xs" />{{ u("keyNote") }}</p>

  <ConnectionSheet v-if="sheet" :kind="sheet.kind" :connection="sheet.connection" @close="sheet = null" @saved="saved" />
  <AppDialog v-if="removing" :title="u('deleteConnTitle')" :description="inUse && replacements.length ? u('deleteConnInUse') : u('deleteConnDesc')" :busy="removeBusy" @close="removing = null">
    <p class="sm medium">{{ removing.model }} <span class="muted" style="font-weight: 400">· {{ providerName(removing.provider) }}</span></p>
    <div v-if="inUse && replacements.length" class="field"><span class="label">{{ u("replacement") }}</span><UiSelect v-model="replacement" :options="replacements" :label="u('replacement')" :placeholder="u('replacement')" /></div>
    <p v-if="removeError" class="help danger-text" role="alert">{{ uiError(removeError) }}</p>
    <template #footer>
      <button class="btn btn-outline btn-sm" type="button" :disabled="removeBusy" @click="removing = null">{{ u("cancel") }}</button>
      <button class="btn btn-destructive btn-sm" type="button" :disabled="removeBusy || (inUse && replacements.length > 0 && !replacement)" @click="remove">{{ u("remove") }}</button>
    </template>
  </AppDialog>
</template>
