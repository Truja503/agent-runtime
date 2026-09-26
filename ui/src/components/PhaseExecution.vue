<script setup lang="ts">
import { computed } from "vue";
import type { PhaseExecution } from "../types";
const props = defineProps<{ execution: PhaseExecution; taskStatus: string }>();
const active = computed(() => props.execution.phases[props.execution.current_phase_index]);
const plan = computed(() => props.execution.plan.phases[props.execution.current_phase_index]);
function files(requirements: Record<string, unknown>): string[] {
  return [...new Set(Object.entries(requirements)
    .filter(([key, value]) => key.includes("file") && Array.isArray(value))
    .flatMap(([, value]) => value as string[]))];
}
</script>
<template>
  <section class="phase-execution notice" aria-label="Project plan and phase execution">
    <h3>Project plan</h3>
    <p>{{ execution.plan.summary }}</p>
    <p v-if="['failed', 'stalled', 'paused', 'interrupted'].includes(taskStatus)" class="error">
      {{ taskStatus.toUpperCase() }} IN PHASE {{ execution.current_phase_index + 1 }}/{{ execution.phases.length }} · {{ plan.title.toUpperCase() }}
    </p>
    <ol class="phase-plan">
      <li v-for="(phase, index) in execution.plan.phases" :key="phase.id">
        <details :open="index === execution.current_phase_index">
          <summary>
            {{ execution.phases[index].status === 'passed' ? '✓' : index === execution.current_phase_index ? '→' : '○' }}
            {{ index + 1 }} · {{ phase.title }} — {{ execution.phases[index].status.toUpperCase() }}
            · {{ execution.phases[index].attempt }} attempt(s)
          </summary>
          <p><code>{{ phase.id }}</code> · {{ phase.goal }}</p>
          <p>Workers: {{ phase.workers.join(' → ') }}</p>
          <ul v-if="files(phase.requirements).length">
            <li v-for="file in files(phase.requirements)" :key="file">
              {{ execution.phases[index].evidence.verified_files?.includes(file) ? '✓' : '✗' }} {{ file }}
            </li>
          </ul>
          <p v-for="check in phase.verification" :key="check">
            {{ check }}: {{ execution.phases[index].evidence.verification?.[check] || 'pending' }}
          </p>
          <dl>
            <template v-for="(requirement, key) in phase.requirements" :key="key">
              <template v-if="requirement && (!Array.isArray(requirement) || requirement.length)">
                <dt>{{ String(key).replaceAll('_', ' ') }}</dt><dd>{{ requirement }}</dd>
              </template>
            </template>
          </dl>
        </details>
      </li>
    </ol>
    <div v-if="active">
      <strong>PHASE {{ execution.current_phase_index + 1 }}/{{ execution.phases.length }} · {{ plan.title }}</strong>
      <p>{{ active.status.toUpperCase() }} · {{ active.active_worker || 'Runtime verification' }} · attempt {{ active.attempt }}</p>
      <p>Last meaningful progress: {{ active.last_meaningful_progress || 'None recorded' }}</p>
      <p v-if="active.files_changed.length">Changed: {{ active.files_changed.join(', ') }}</p>
      <ul v-if="active.outstanding.length" aria-label="Outstanding phase requirements">
        <li v-for="finding in active.outstanding" :key="finding" class="error">{{ finding }}</li>
      </ul>
      <p v-if="active.pending_approvals.length">Waiting for operator: {{ active.pending_approvals.join(', ') }}</p>
      <p v-if="active.stop_reason">{{ active.stop_reason }}</p>
      <p v-if="['paused', 'stalled', 'failed'].includes(taskStatus)">Resume validates checkpoints and continues from this phase.</p>
    </div>
  </section>
</template>
