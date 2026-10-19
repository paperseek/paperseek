<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { ApiError } from "../../api/client";
import type { SourceDescriptor } from "../../api/types";
import Icon from "../../components/Icon.vue";
import UiSelect from "../../components/UiSelect.vue";
import UiSheet from "../../components/UiSheet.vue";
import { toast } from "../../components/toast";
import { useLocale } from "../../locales";
import { useUi, useUiError } from "../../locales/ui";
import { useDates } from "../workspace/format";
import { loadSettings, settingsApi, sourceSetting, workspaceSettings, type Probe, type ProxyScope } from "./store";

const { sourceName, t } = useLocale();
const u = useUi();
const uiError = useUiError();
const { ago } = useDates();
const testing = reactive(new Set<string>());
const editing = ref<SourceDescriptor | null>(null);
const form = reactive({ api_key: "", clear: false, options: {} as Record<string, string> });
const showKey = ref(false);
const busy = ref(false);
const sheetProbe = ref<Probe | null>(null);
const sheetError = ref("");
const descriptors = computed(() => workspaceSettings.descriptors);
const defaultSource = computed({
  get: () => workspaceSettings.defaults?.source || "",
  set: async (value: string) => {
    try {
      workspaceSettings.defaults = await settingsApi.saveDefaults({ source: value });
      toast(u("savedToast"), { tone: "ok" });
    } catch {
      toast(u("actionFailed"), { tone: "fail" });
    }
  },
});
const sourceOptions = computed(() => descriptors.value.filter((item) => sourceSetting(item.id)?.usable).map((item) => ({ value: item.id, label: sourceName(item.id, item.label) })));
const optionLabels: Record<string, () => string> = {
  openalex_email: () => u("contactEmail"), crossref_email: () => u("contactEmail"),
  pubmed_tool: () => u("pubmedTool"), pubmed_email: () => u("pubmedEmail"), wos_db: () => u("wosDb"),
};
const abbreviations: Record<string, string> = {
  openalex: "OA", semanticscholar: "S2", pubmed: "PM", arxiv: "ar", crossref: "CR", paperhub: "CS", wos: "WS", googlescholar: "GS",
};
function initials(item: SourceDescriptor) {
  return abbreviations[item.id] || item.label.slice(0, 2);
}
function keyText(item: SourceDescriptor) {
  const setting = sourceSetting(item.id);
  if (item.api_key === "not_required") return { main: u("keyNotNeeded"), sub: "", muted: true, alert: false };
  return {
    main: setting?.has_key ? (setting.key_origin === "environment" ? u("keyEnv") : u("keySet")) : u("keyUnset"),
    sub: u(`req_${item.api_key}` as "req_optional"),
    muted: !setting?.has_key,
    alert: item.api_key === "required" && !setting?.has_key,
  };
}
function optionsText(item: SourceDescriptor) {
  const options = sourceSetting(item.id)?.options || {};
  const entries = Object.entries(options);
  if (!entries.length) return "—";
  return entries.map(([key, value]) => `${optionLabels[key]?.() || key} · ${value || u("notFilled")}`).join("；");
}
async function test(id: string) {
  testing.add(id);
  try {
    await settingsApi.testSource(id);
  } catch {
    /* Recorded result is shown after reload. */
  } finally {
    testing.delete(id);
    await loadSettings(true);
  }
}
async function testAll() {
  await Promise.all(descriptors.value.filter((item) => sourceSetting(item.id)?.usable).map((item) => test(item.id)));
}
function configure(item: SourceDescriptor) {
  editing.value = item;
  form.api_key = "";
  form.clear = false;
  form.options = { ...(sourceSetting(item.id)?.options || {}) };
  if (item.id === "pubmed" && !form.options.pubmed_tool) form.options.pubmed_tool = "paperseek";
  sheetProbe.value = null;
  sheetError.value = "";
  showKey.value = false;
}
async function saveSource(close = true) {
  if (!editing.value) return false;
  busy.value = true;
  sheetError.value = "";
  try {
    await settingsApi.saveSource(editing.value.id, {
      api_key: form.clear ? "" : form.api_key.trim() || null,
      options: Object.keys(form.options).length ? form.options : null,
    });
    await loadSettings(true);
    if (close) {
      editing.value = null;
      toast(u("savedToast"), { tone: "ok" });
    }
    return true;
  } catch (e) {
    sheetError.value = e instanceof ApiError ? e.code : "generic";
    return false;
  } finally {
    busy.value = false;
  }
}
// Network proxy: addresses are write-only; the page only ever sees host:port.
const scopes: ProxyScope[] = ["models", "ip_limited_sources", "other_sources"];
const proxy = computed(() => workspaceSettings.proxy);
const proxyForm = reactive({ open: false, urls: "", remove: false, scope: [...scopes] as ProxyScope[], error: "" });
const proxySummary = computed(() => {
  const value = proxy.value;
  if (!value?.hosts.length) return "";
  return u("proxyScopeSummary", { scopes: value.scope.map((scope) => u(`scope_${scope}`)).join(u("listSep")) || "—" });
});
function editProxy() {
  Object.assign(proxyForm, { open: true, urls: "", remove: false, error: "",
    scope: proxy.value?.hosts.length ? [...proxy.value.scope] : [...scopes] });
}
async function saveProxy() {
  if (!proxyForm.remove && !proxyForm.scope.length) {
    proxyForm.error = u("proxyScopeRequired");
    return;
  }
  busy.value = true;
  proxyForm.error = "";
  try {
    workspaceSettings.proxy = await settingsApi.saveProxy({
      urls: proxyForm.remove ? "" : proxyForm.urls.trim() || null,
      scope: proxyForm.scope,
    });
    proxyForm.open = false;
    toast(u("savedToast"), { tone: "ok" });
  } catch (e) {
    proxyForm.error = uiError(e instanceof ApiError ? e.code : "generic");
  } finally {
    busy.value = false;
  }
}
async function testSheet() {
  if (!editing.value || !(await saveSource(false))) return;
  form.api_key = "";
  form.clear = false;
  sheetProbe.value = await settingsApi.testSource(editing.value.id).catch(() => ({ ok: false, latency_ms: null, code: "request_failed" }));
  await loadSettings(true);
}
</script>

<template>
  <div class="toolbar">
    <UiSelect v-model="defaultSource" :options="sourceOptions" :label="u('defaultSource')" :prefix="u('defaultSource')" size="sm" inline />
    <span class="xs muted" style="margin-left: 4px">{{ u("sourcesNote") }}</span>
    <div class="row end"><button class="btn btn-outline btn-sm" type="button" :disabled="testing.size > 0" @click="testAll"><Icon v-if="testing.size" name="loader" />{{ u("testAll") }}</button></div>
  </div>
  <div class="table-wrap">
    <table class="table">
      <thead>
        <tr><th>{{ u("colSourceName") }}</th><th class="shrink">{{ u("colKey") }}</th><th class="shrink">{{ u("colStatus") }}</th><th>{{ u("colSourceOptions") }}</th><th class="shrink"><span class="sr-only">{{ u("colActions") }}</span></th></tr>
      </thead>
      <tbody>
        <tr v-for="item in descriptors" :key="item.id" :class="{ off: !sourceSetting(item.id)?.usable }">
          <td><div class="row" style="gap: 10px; flex-wrap: nowrap"><span class="prov">{{ initials(item) }}</span><span class="stack"><span class="medium">{{ sourceName(item.id, item.label) }}</span><span v-if="item.capabilities.citation_expansion" class="xs muted">{{ u("citationSupport") }}</span><span v-else-if="item.status === 'temporarily_unavailable'" class="xs muted">{{ u("srcTempUnavailable") }}</span></span></div></td>
          <td><span class="sm" :class="{ muted: keyText(item).muted && !keyText(item).alert }">{{ keyText(item).main }}</span><p v-if="keyText(item).sub" class="xs" :class="{ muted: !keyText(item).alert }" :style="keyText(item).alert ? 'color: var(--destructive)' : undefined">{{ keyText(item).sub }}</p></td>
          <td>
            <span v-if="testing.has(item.id)" class="row sm muted" style="gap: 6px; flex-wrap: nowrap"><Icon name="loader" />{{ u("testing") }}</span>
            <span v-else-if="!sourceSetting(item.id)?.usable" class="row sm" style="gap: 6px; flex-wrap: nowrap"><Icon name="circle-minus" />{{ u("srcUnusable") }}</span>
            <div v-else-if="sourceSetting(item.id)?.test.status !== 'untested'" class="stack" style="gap: 2px">
              <span class="row sm" style="gap: 6px; flex-wrap: nowrap" :style="sourceSetting(item.id)?.test.status === 'failed' ? 'color: var(--destructive)' : undefined"><Icon :name="sourceSetting(item.id)?.test.status === 'ok' ? 'ok' : 'fail'" />{{ u(sourceSetting(item.id)?.test.status === "ok" ? "connOk" : "connFailed") }}</span>
              <span class="xs muted num" style="padding-left: 22px">{{ sourceSetting(item.id)?.test.status === "ok" ? `${sourceSetting(item.id)?.test.latency_ms ?? 0} ms` : uiError(sourceSetting(item.id)?.test.code || "") }} · {{ ago(sourceSetting(item.id)?.test.tested_at) }}</span>
            </div>
            <span v-else class="row sm" style="gap: 6px; flex-wrap: nowrap"><Icon name="ok" />{{ u("srcUsable") }}</span>
          </td>
          <td class="sm wrap" :class="{ muted: optionsText(item) === '—' || sourceSetting(item.id)?.usable }" style="min-width: 160px">{{ optionsText(item) }}</td>
          <td>
            <button v-if="!sourceSetting(item.id)?.usable" class="btn btn-sm" type="button" style="height: 28px" @click="configure(item)">{{ u("configureKey") }}</button>
            <div v-else class="row" style="gap: 2px; flex-wrap: nowrap">
              <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="testing.has(item.id)" @click="test(item.id)">{{ u("test") }}</button>
              <button v-if="item.api_key !== 'not_required' || Object.keys(sourceSetting(item.id)?.options || {}).length" class="btn btn-outline btn-sm" type="button" style="height: 28px" @click="configure(item)">{{ u("configure") }}</button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <p class="xs muted row" style="gap: 6px"><Icon name="lock" size="xs" />{{ u("sourcesFoot") }}</p>

  <!-- Only a server that exposes proxy settings (Community) returns them. -->
  <section v-if="proxy" class="card c" aria-labelledby="proxy-title" style="margin-top: 24px">
    <div class="card-header"><div><h2 id="proxy-title" class="card-title">{{ u("proxyTitle") }}</h2><p class="help" style="margin-top: 4px">{{ u("proxyDesc") }}</p></div></div>
    <div class="card-content stack" style="gap: 0">
      <div class="setrow">
        <span class="stack" style="gap: 2px">
          <template v-if="proxy?.hosts.length">
            <span class="sm medium">{{ u("proxyCount", { n: proxy.hosts.length }) }} <span class="muted num" style="font-weight: 400">· {{ proxy.hosts.join(", ") }}</span></span>
            <span class="xs muted">{{ proxySummary }}</span>
            <span v-if="proxy.origin === 'environment'" class="xs muted">{{ u("proxyEnv") }}</span>
          </template>
          <span v-else class="sm muted">{{ u("proxyNone") }}</span>
        </span>
        <button v-if="proxy?.origin !== 'environment'" class="btn btn-outline btn-sm" type="button" style="height: 28px" @click="editProxy">{{ proxy?.hosts.length ? u("configure") : u("proxySetUp") }}</button>
      </div>
    </div>
  </section>

  <UiSheet v-if="proxyForm.open" :title="u('proxyTitle')" :description="u('proxySheetDesc')" :busy="busy" @close="proxyForm.open = false">
    <form id="proxy-form" class="stack" style="gap: 16px" autocomplete="off" @submit.prevent="saveProxy">
      <div class="field">
        <label class="label" for="proxy-urls">{{ u("proxyUrls") }}</label>
        <textarea id="proxy-urls" v-model="proxyForm.urls" class="input textarea" rows="4" spellcheck="false" maxlength="8192" :disabled="proxyForm.remove" placeholder="http://user:password@proxy.example:8080"></textarea>
        <p class="help">
          {{ u("proxyUrlsHelp") }}
          <template v-if="proxy?.origin === 'settings'"><br />{{ u("proxyUrlsKeep", { n: proxy.hosts.length }) }} · <button type="button" class="link" @click="proxyForm.remove = !proxyForm.remove">{{ proxyForm.remove ? u("cancel") : u("proxyRemove") }}</button></template>
        </p>
      </div>
      <fieldset class="field" :disabled="proxyForm.remove" style="border: 0; padding: 0; margin: 0">
        <legend class="label">{{ u("proxyScope") }}</legend>
        <label v-for="scope in scopes" :key="scope" class="cb" style="align-items: flex-start; margin-top: 10px">
          <input v-model="proxyForm.scope" type="checkbox" :value="scope" /><span class="box" style="margin-top: 2px"><Icon name="check" /></span>
          <span class="stack" style="gap: 2px"><span class="sm medium">{{ u(`scope_${scope}`) }}</span><span class="xs muted">{{ u(`scope_${scope}_desc`) }}</span></span>
        </label>
        <p class="help" style="margin-top: 10px">{{ u("proxyScopeHelp") }}</p>
      </fieldset>
      <p v-if="proxyForm.error" class="help danger-text" role="alert">{{ proxyForm.error }}</p>
    </form>
    <template #footer>
      <button class="btn btn-outline btn-sm" type="button" :disabled="busy" @click="proxyForm.open = false">{{ u("cancel") }}</button>
      <button class="btn btn-sm" type="submit" form="proxy-form" :disabled="busy">{{ u("save") }}</button>
    </template>
  </UiSheet>

  <UiSheet v-if="editing" :title="u('configureSource', { name: sourceName(editing.id, editing.label) })" :description="u('configureSourceDesc')" :busy="busy" @close="editing = null">
    <form id="source-form" class="stack" style="gap: 16px" autocomplete="off" @submit.prevent="saveSource()">
      <div v-if="editing.credential" class="field">
        <label class="label" for="source-key">{{ editing.credential.input_label || u("apiKey") }} <span class="muted" style="font-weight: 400">· {{ u(`req_${editing.api_key}` as "req_optional") }}</span></label>
        <div class="ig">
          <input id="source-key" v-model="form.api_key" :type="showKey ? 'text' : 'password'" spellcheck="false" :placeholder="sourceSetting(editing.id)?.key_origin === 'settings' && !form.clear ? '••••••••' : ''" :disabled="form.clear" />
          <button class="btn btn-ghost btn-icon btn-xs" type="button" :aria-label="u(showKey ? 'hideKey' : 'showKey')" style="margin-right: 4px" @click="showKey = !showKey"><Icon :name="showKey ? 'eye-off' : 'eye'" /></button>
        </div>
        <p class="help">
          <template v-if="sourceSetting(editing.id)?.key_origin === 'settings'">{{ u("apiKeyKeep") }} · <button type="button" class="link" @click="form.clear = !form.clear">{{ form.clear ? u("cancel") : u("clearKey") }}</button> · </template>
          <a class="link" :href="editing.credential.guidance_url" target="_blank" rel="noopener">{{ u("getKey") }}</a>
        </p>
      </div>
      <div v-for="(_, key) in form.options" :key="key" class="field">
        <label class="label" :for="`source-${key}`">{{ optionLabels[key]?.() || key }}</label>
        <input :id="`source-${key}`" v-model="form.options[key]" class="input" :type="String(key).endsWith('email') ? 'email' : 'text'" maxlength="320" />
        <p v-if="String(key).endsWith('email')" class="help">{{ u("contactEmailHelp") }}</p>
      </div>
      <div v-if="sheetProbe" class="alert" role="status">
        <Icon :name="sheetProbe.ok ? 'ok' : 'fail'" />
        <p class="alert-title">{{ u(sheetProbe.ok ? "testOk" : "testFail") }}</p>
        <p class="alert-desc">{{ sheetProbe.ok ? `${sourceName(editing.id, editing.label)} · ${sheetProbe.latency_ms ?? 0} ms` : uiError(sheetProbe.code || '') }}</p>
        <div class="alert-act"><button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="busy" @click="testSheet">{{ u("retest") }}</button></div>
      </div>
      <button v-else class="btn btn-outline btn-sm" type="button" style="align-self: flex-start" :disabled="busy" @click="testSheet"><Icon v-if="busy" name="loader" />{{ u("testConnection") }}</button>
      <p v-if="sheetError" class="help danger-text" role="alert">{{ t(sheetError) === sheetError ? uiError(sheetError) : t(sheetError) }}</p>
    </form>
    <template #footer>
      <button class="btn btn-outline btn-sm" type="button" :disabled="busy" @click="editing = null">{{ u("cancel") }}</button>
      <button class="btn btn-sm" type="submit" form="source-form" :disabled="busy">{{ u("save") }}</button>
    </template>
  </UiSheet>
</template>
