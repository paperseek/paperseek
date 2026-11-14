<script setup lang="ts">
import { ref, watch } from "vue";
import { toast } from "../../components/toast";
import { useUi } from "../../locales/ui";
import { settingsApi, workspaceSettings } from "./store";

const u = useUi();
const email = ref("");
const busy = ref(false);
// Only a server that offers the setting (Community) returns the field; elsewhere the section stays hidden.
watch(() => workspaceSettings.defaults?.unpaywall_email, (value) => (email.value = value ?? ""), { immediate: true });

async function save() {
  busy.value = true;
  try {
    workspaceSettings.defaults = await settingsApi.saveDefaults({ unpaywall_email: email.value.trim() });
    toast(u("savedToast"), { tone: "ok" });
  } catch {
    toast(u("actionFailed"), { tone: "fail" });
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section v-if="workspaceSettings.defaults?.unpaywall_email != null" class="card c" aria-labelledby="unpaywall-title" style="margin-top: 24px" data-testid="open-access-links">
    <div class="card-header">
      <div><h2 id="unpaywall-title" class="card-title">{{ u("unpaywallTitle") }}</h2><p class="help" style="margin-top: 4px">{{ u("unpaywallDesc") }}</p></div>
    </div>
    <form class="card-content field" autocomplete="off" @submit.prevent="save">
      <label class="label" for="unpaywall-email">{{ u("unpaywallEmail") }}</label>
      <div class="row" style="gap: 8px; flex-wrap: nowrap">
        <input id="unpaywall-email" v-model="email" class="input" type="email" maxlength="320" style="max-width: 360px" />
        <button class="btn btn-sm" type="submit" :disabled="busy || email.trim() === (workspaceSettings.defaults?.unpaywall_email ?? '')">{{ u("save") }}</button>
      </div>
      <p class="help">{{ u("unpaywallEmailHelp") }}</p>
    </form>
  </section>
  <p v-if="workspaceSettings.defaults?.unpaywall_email != null" class="xs muted" style="margin-top: 8px">{{ u("unpaywallFoot") }}</p>
</template>
