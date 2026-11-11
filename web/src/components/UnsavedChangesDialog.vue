<script setup lang="ts">
import AppDialog from "./AppDialog.vue";
import { useUi } from "../locales/ui";
defineProps<{ open: boolean }>();
const emit = defineEmits<{ decide: [leave: boolean] }>();
const u = useUi();
</script>
<template>
  <AppDialog
    v-if="open"
    :title="u('unsavedTitle')"
    :description="u('unsavedDesc')"
    @close="emit('decide', false)"
  >
    <template #footer>
      <button class="btn btn-outline btn-sm" type="button" @click="emit('decide', true)">{{ u("discardChanges") }}</button>
      <button class="btn btn-sm" type="button" @click="emit('decide', false)">{{ u("keepEditing") }}</button>
    </template>
  </AppDialog>
</template>
