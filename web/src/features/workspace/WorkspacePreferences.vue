<script setup lang="ts">
import { computed, onMounted, watchEffect } from "vue";
import { useRoute } from "vue-router";
import Icon from "../../components/Icon.vue";
import UiSelect from "../../components/UiSelect.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import NetworkProxy from "../settings/NetworkProxy.vue";
import SettingsModels from "../settings/SettingsModels.vue";
import SettingsSources from "../settings/SettingsSources.vue";
import SettingsUser from "../settings/SettingsUser.vue";
import { loadSettings, workspaceSettings } from "../settings/store";
import { setHeader } from "./state";

const route = useRoute();
const u = useUi();
const { language, setLanguage } = useLocale();
const section = computed(() => (["models", "sources", "general", "user"].includes(String(route.params.section)) ? String(route.params.section) : "models") as "models" | "sources" | "general" | "user");
const usable = computed(() => workspaceSettings.sources.filter((item) => item.usable).length);
const languageValue = computed({ get: () => language.value, set: (value: "zh" | "en") => setLanguage(value) });
watchEffect(() => setHeader(u("settings")));
onMounted(() => void loadSettings(true));
</script>

<template>
  <main class="page">
    <nav class="tabs" role="tablist" :aria-label="u('settings')">
      <RouterLink class="tab" :class="{ active: section === 'models' }" role="tab" :aria-selected="section === 'models'" to="/settings/models">{{ u("tab_models") }}<span v-if="workspaceSettings.loaded" class="badge badge-secondary">{{ workspaceSettings.connections.length }}</span></RouterLink>
      <RouterLink class="tab" :class="{ active: section === 'sources' }" role="tab" :aria-selected="section === 'sources'" to="/settings/sources">{{ u("tab_sources") }}<span v-if="workspaceSettings.loaded" class="badge badge-secondary">{{ usable }} / {{ workspaceSettings.sources.length }}</span></RouterLink>
      <RouterLink class="tab" :class="{ active: section === 'general' }" role="tab" :aria-selected="section === 'general'" to="/settings/general">{{ u("tab_general") }}</RouterLink>
      <RouterLink class="tab" :class="{ active: section === 'user' }" role="tab" :aria-selected="section === 'user'" to="/settings/user">{{ u("tab_user") }}</RouterLink>
    </nav>
    <div v-if="workspaceSettings.error" class="alert" role="alert"><Icon name="fail" /><p class="alert-title">{{ u("loadError") }}</p><div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="loadSettings(true)">{{ u("retry") }}</button></div></div>
    <SettingsModels v-else-if="section === 'models'" />
    <SettingsSources v-else-if="section === 'sources'" />
    <SettingsUser v-else-if="section === 'user'" />
    <template v-else>
      <section class="card c" style="max-width: 880px" aria-labelledby="general-title">
        <div class="card-header"><div><h2 id="general-title" class="card-title">{{ u("language") }}</h2><p class="help" style="margin-top: 4px">{{ u("generalDesc") }}</p></div></div>
        <div class="card-content" style="max-width: 280px">
          <UiSelect v-model="languageValue" :options="[{ value: 'zh', label: '简体中文' }, { value: 'en', label: 'English' }]" :label="u('language')" />
        </div>
      </section>
      <NetworkProxy />
    </template>
  </main>
</template>
