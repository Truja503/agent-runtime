<script setup lang="ts">
import { ref, watch } from 'vue'
import type { Agent } from '../types'
const props = defineProps<{agent: Agent; editable: boolean}>()
const emit = defineEmits<{ save: [mandate: string]; close: [] }>()
const mandate = ref(props.agent.mandate)
watch(() => props.agent.id, () => {mandate.value = props.agent.mandate})
</script>
<template><aside class="inspector panel"><div class="section-top"><h2>{{agent.name}}</h2><button @click="emit('close')" aria-label="Close inspector">×</button></div><p class="eyebrow">AGENT INSPECTOR</p><dl><dt>Profile</dt><dd>{{agent.profile}}</dd><dt>Model</dt><dd>{{agent.model}}</dd><dt>Provider</dt><dd>{{agent.provider}} · {{agent.location}}</dd><dt>Endpoint</dt><dd>{{agent.endpoint}}</dd><dt>Output limit</dt><dd>{{agent.max_tokens}}</dd><dt>Risk ceiling</dt><dd>{{agent.risk_limit}}</dd><dt>Current task</dt><dd>{{agent.current_task || 'Idle'}}</dd></dl><h3>Allowed tools</h3><div class="chips"><code v-for="tool in agent.tools" :key="tool">{{tool}}</code><span v-if="!agent.tools.length">No tool authority</span></div><h3>System / security rules <span class="muted">Read-only</span></h3><pre>{{agent.security_rules}}</pre><p class="muted">Authority is enforced by code and policy, regardless of prompt text.</p><label>Agent mandate / behavior<textarea v-model="mandate" rows="6" :disabled="!editable" maxlength="8000" /></label><button :disabled="!editable" @click="emit('save', mandate)">Save mandate</button><details><summary>Actual assembled system template</summary><pre>{{agent.system_prompt}}</pre></details></aside></template>
