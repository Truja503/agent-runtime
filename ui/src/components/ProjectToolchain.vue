<script setup lang="ts">
import { ref, watch } from "vue";
import { api, pretty } from "../api";
const props = defineProps<{ project?: string | null; editable: boolean; checkpoint?: string }>();
const details = ref<Record<string, unknown> | null>(null), error = ref("");
const requests = ref<{request_id: string; project: string; status: string;
  manifest: unknown; additional_python: string[]; additional_npm: string[]; installing?: boolean;
  reason?: string; result?: unknown; retryable_infrastructure?: boolean}[]>([]);
const operator = ref(""), secret = ref(""), additional = ref(false), busy = ref(false);
async function refresh() {
  if (!props.project) return;
  try {
    details.value = await api("/project-toolchain/inspect", "POST", {project: props.project});
    requests.value = (await api<typeof requests.value>("/project-toolchain/approvals"))
      .filter(r => r.project === props.project && ["awaiting_approval", "failed"].includes(r.status));
    error.value = "";
  } catch (e) { error.value = String(e); }
}
async function decide(id: string, approve: boolean, retry = false) {
  busy.value = true;
  try {
    await api(`/project-toolchain/approvals/${id}`, "POST", {approve, allow_additional_packages: additional.value, ...(retry ? {retry_infrastructure: true} : {})},
      {"X-Operator-Id": operator.value, "X-Operator-Secret": secret.value});
    secret.value = "";
    await refresh();
  } catch (e) { error.value = String(e); }
  finally { secret.value = ""; busy.value = false; }
}
watch(() => [props.project, props.checkpoint], refresh, {immediate: true});
</script>
<template>
  <section v-if="project" class="panel">
    <div class="section-top"><h3>Project Toolchain</h3><button @click="refresh">Refresh</button></div>
    <p v-if="error" class="error">{{ error }}</p>
    <template v-if="details">
      <p>Framework: {{ details.framework }} · Environment: {{ details.environment }}</p>
      <p>Server: {{ details.server }}</p>
      <details><summary>Dependencies</summary><pre>{{ pretty(details.dependencies) }}</pre></details>
      <details><summary>Build status</summary><pre>{{ pretty(details.build) }}</pre></details>
      <details><summary>Test status</summary><pre>{{ pretty(details.test) }}</pre></details>
      <details><summary>Docker readiness</summary><pre>{{ pretty(details.executor) }}</pre></details>
    </template>
    <section v-for="request in requests" :key="request.request_id" class="notice">
      <strong>{{ request.status === 'failed' ? 'Dependency installation failed' : request.installing ? 'Installing approved dependencies' : 'Dependency approval required' }}</strong>
      <pre v-if="request.status === 'failed'">{{ request.reason || pretty(request.result) }}</pre>
      <pre>{{ pretty(request.manifest) }}</pre>
      <p v-if="request.additional_python.length || request.additional_npm.length">
        Additional packages: {{ [...request.additional_python, ...request.additional_npm].join(', ') }}
      </p>
      <template v-if="request.status === 'awaiting_approval' || request.retryable_infrastructure">
      <label>Operator ID<input v-model="operator" autocomplete="off" /></label>
      <label>Operator secret<input v-model="secret" type="password" autocomplete="off" /></label>
      <label><input v-model="additional" type="checkbox" /> Explicitly approve listed additional packages</label>
      <button :disabled="!editable || busy || !operator || !secret || request.installing" @click="decide(request.request_id, true, request.status === 'failed')">{{ request.status === 'failed' ? 'Retry infrastructure failure with operator approval' : 'Approve exact manifest and install' }}</button>
      <button v-if="request.status === 'awaiting_approval'" :disabled="!editable || busy || !operator || !secret || request.installing" @click="decide(request.request_id, false)">Deny</button>
      </template>
    </section>
  </section>
</template>
