<script setup lang="ts">
import type { RuntimeState } from "../types";
defineProps<{ browser?: RuntimeState["browser"]; editable: boolean }>();
defineEmits<{ check: [] }>();
</script>
<template>
  <div class="workspace-bar">
    <strong>Local Browser QA</strong>
    <span class="badge">{{ browser?.status === "READY" && browser.launch_test ? "READY" : "UNAVAILABLE" }}</span>
    <details v-if="browser?.error"><summary>Browser diagnostics</summary><pre>{{ browser.error }}</pre></details>
    <span v-else-if="!browser" class="muted">Not checked</span>
    <button :disabled="!editable" @click="$emit('check')">Check browser</button>
  </div>
</template>
