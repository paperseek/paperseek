<script setup lang="ts">
// /search renders the create page; /runs/:id renders the run page.
import RunPage from "./RunPage.vue";
import SearchCreate from "./SearchCreate.vue";

withDefaults(defineProps<{ mode: "edit" | "run"; submissionEnabled?: boolean; singleRun?: boolean; externalRecovery?: boolean }>(), { submissionEnabled: true });
</script>

<template>
  <SearchCreate v-if="mode === 'edit'" :submission-enabled="submissionEnabled" :single-run="singleRun">
    <template v-if="$slots.notice" #notice><slot name="notice" /></template>
    <template v-if="$slots.setup" #setup="scope"><slot name="setup" v-bind="scope" /></template>
    <template v-if="$slots.service" #service="scope"><slot name="service" v-bind="scope" /></template>
    <template v-if="$slots.cost" #cost><slot name="cost" /></template>
  </SearchCreate>
  <RunPage v-else :external-recovery="externalRecovery" />
</template>
