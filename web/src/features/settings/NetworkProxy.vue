<script setup lang="ts">
// Settings › General: network proxy. Addresses are write-only; the page only ever sees host:port.
import { computed, reactive, ref } from "vue";
import { ApiError } from "../../api/client";
import Icon from "../../components/Icon.vue";
import UiSheet from "../../components/UiSheet.vue";
import { toast } from "../../components/toast";
import { useUi, useUiError } from "../../locales/ui";
import { settingsApi, workspaceSettings, type ProxyScope } from "./store";

const u = useUi();
const uiError = useUiError();
const busy = ref(false);
const scopes: ProxyScope[] = ["models", "ip_limited_sources", "other_sources"];
const proxy = computed(() => workspaceSettings.proxy);
const proxyForm = reactive({ open: false, urls: "", scope: [...scopes] as ProxyScope[], error: "" });
const proxySummary = computed(() => {
  const value = proxy.value;
  if (!value?.hosts.length) return "";
  return u("proxyScopeSummary", { scopes: value.scope.map((scope) => u(`scope_${scope}`)).join(u("listSep")) || "—" });
});
function editProxy() {
  Object.assign(proxyForm, { open: true, urls: "", error: "",
    scope: proxy.value?.hosts.length ? [...proxy.value.scope] : [...scopes] });
}
async function removeProxy() {
  busy.value = true;
  try {
    workspaceSettings.proxy = await settingsApi.saveProxy({ urls: "", scope: proxy.value?.scope || [...scopes] });
    proxyForm.open = false;
    toast(u("proxyRemoved"), { tone: "ok" });
  } catch (e) {
    toast(uiError(e instanceof ApiError ? e.code : "generic"), { tone: "fail" });
  } finally {
    busy.value = false;
  }
}
async function saveProxy() {
  if (!proxyForm.scope.length) {
    proxyForm.error = u("proxyScopeRequired");
    return;
  }
  busy.value = true;
  proxyForm.error = "";
  try {
    workspaceSettings.proxy = await settingsApi.saveProxy({
      urls: proxyForm.urls.trim() || null,
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
</script>

<template>
  <!-- Only a server that exposes proxy settings (Community) returns them. -->
  <section v-if="proxy" class="card c" aria-labelledby="proxy-title" style="max-width: 880px; margin-top: 24px">
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
        <div v-if="proxy?.origin !== 'environment'" class="row" style="gap: 2px; flex-wrap: nowrap">
          <button v-if="proxy?.origin === 'settings' && proxy.hosts.length" class="btn btn-ghost btn-sm" type="button" style="height: 28px" data-testid="proxy-remove" :disabled="busy" @click="removeProxy">{{ u("proxyRemove") }}</button>
          <button class="btn btn-outline btn-sm" type="button" style="height: 28px" :disabled="busy" @click="editProxy">{{ proxy?.hosts.length ? u("configure") : u("proxySetUp") }}</button>
        </div>
      </div>
    </div>
  </section>

  <UiSheet v-if="proxyForm.open" :title="u('proxyTitle')" :description="u('proxySheetDesc')" :busy="busy" @close="proxyForm.open = false">
    <form id="proxy-form" class="stack" style="gap: 16px" autocomplete="off" @submit.prevent="saveProxy">
      <div class="field">
        <label class="label" for="proxy-urls">{{ u("proxyUrls") }}</label>
        <textarea id="proxy-urls" v-model="proxyForm.urls" class="input textarea" rows="4" spellcheck="false" maxlength="8192" placeholder="http://user:password@proxy.example:8080"></textarea>
        <p class="help">
          {{ u("proxyUrlsHelp") }}
          <template v-if="proxy?.origin === 'settings' && proxy.hosts.length"><br />{{ u("proxyUrlsKeep", { n: proxy.hosts.length }) }}</template>
        </p>
      </div>
      <fieldset class="field" style="border: 0; padding: 0; margin: 0">
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
      <button v-if="proxy?.origin === 'settings' && proxy.hosts.length" class="btn btn-ghost btn-sm danger-text" type="button" style="margin-right: auto" :disabled="busy" @click="removeProxy">{{ u("proxyRemove") }}</button>
      <button class="btn btn-outline btn-sm" type="button" :disabled="busy" @click="proxyForm.open = false">{{ u("cancel") }}</button>
      <button class="btn btn-sm" type="submit" form="proxy-form" :disabled="busy">{{ u("save") }}</button>
    </template>
  </UiSheet>
</template>
