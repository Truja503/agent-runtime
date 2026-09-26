<script setup lang="ts">
import { computed, ref } from "vue";
import { api, pretty, terminal, taskTitle } from "../api";
import type { Acceptance, Review, Evidence, Event, Task, Workflow } from "../types";
import ProjectToolchain from "./ProjectToolchain.vue";
import PhaseExecutionPanel from "./PhaseExecution.vue";
import type { PhaseExecution } from "../types";
const props = defineProps<{
  task: Task;
  events: Event[];
  evidence: Evidence | null;
  editable: boolean;
  inspection: boolean;
}>();
const emit = defineEmits<{ cancel: []; resume: [] }>();
const workflow = computed(() => props.task.result?.workflow as Workflow | undefined);
const phases = computed(() => props.task.result?.phase_execution as PhaseExecution | undefined);
const toolchainProject = computed(() => {
  if (props.task.options.visual_project) return props.task.options.visual_project;
  const request = [...props.events].reverse().find(
    (e) =>
      e.type === "privileged_action_requested" &&
      e.payload.tool === "project.dependencies" &&
      typeof e.payload.project === "string",
  );
  return request ? String(request.payload.project) : null;
});
const qaStatus = computed(() => {
  const qa = workflow.value?.latest_qa;
  if (!qa?.screenshots_generated) return "Not rendered";
  return qa.previews?.some(p => !p.dom_loaded || p.console_errors?.length || p.failed_resources?.length || p.horizontal_overflow)
    ? "Rendered with errors" : "Rendered";
});
const prompts = ref<unknown>(null),
  promptError = ref("");
const workers = computed(() => {
  const recorded = props.task.result?.worker_results;
  if (Array.isArray(recorded))
    return recorded as { agent: string; status: string; steps: number }[];
  const outcomes = new Map<
    string,
    { agent: string; status: string; steps: number }
  >();
  for (const e of props.events) {
    if (
      ["researcher", "coder", "reviewer"].includes(e.actor) &&
      e.type.startsWith("agent_")
    ) {
      outcomes.set(e.actor, {
        agent: e.actor,
        status: String(e.payload.status || "active"),
        steps: Number(e.payload.steps || 0),
      });
    }
  }
  return [...outcomes.values()].map((worker) => ({
    ...worker,
    status:
      worker.status === "active" &&
      ["cancelled", "interrupted", "failed"].includes(props.task.status)
        ? props.task.status
        : worker.status,
  }));
});
const review = computed(
  () => props.task.result?.review as Review | null | undefined,
);
const acceptance = computed(
  () => props.task.result?.acceptance as Acceptance | null | undefined,
);
const timeoutHistory = computed(() =>
  props.events
    .filter((e) => e.type === "model_timeout")
    .map((e) => {
      const later = props.events.slice(props.events.indexOf(e) + 1);
      const recovered = later.some(
        (x) => x.actor === e.actor && x.type === "model_response",
      );
      return {
        agent: e.actor,
        attempt: e.payload.attempt,
        seconds: e.payload.timeout_seconds,
        result: recovered
          ? "Recovered: a later model response succeeded"
          : "No later successful response recorded",
      };
    }),
);
const warnings = computed(() =>
  props.events.filter((e) =>
    [
      "model_invalid_response",
      "model_retry",
      "model_timeout",
      "model_failed",
      "tool_denied",
      "tool_failed",
    ].includes(e.type),
  ),
);
const duration = computed(() =>
  Math.max(
    0,
    (Date.parse(props.task.updated_at) - Date.parse(props.task.created_at)) /
      1000,
  ).toFixed(1),
);
async function loadPrompts() {
  try {
    prompts.value = await api(`/tasks/${props.task.id}/prompts`);
  } catch (e) {
    promptError.value = String(e);
  }
}
</script>
<template>
  <section class="panel task-detail">
    <div class="section-top">
      <div>
        <p class="eyebrow">TASK / {{ task.id.slice(0, 8) }}</p>
        <h2>{{ taskTitle(task) }}</h2>
      </div>
      <span class="badge" :class="task.status">{{
        task.status.replaceAll("_", " ")
      }}</span>
    </div>
    <details><summary>Task instructions</summary><pre>{{ task.goal }}</pre></details>
    <PhaseExecutionPanel v-if="phases" :execution="phases" :task-status="task.status" />
    <button v-if="phases && task.status === 'failed'" :disabled="!editable" @click="emit('resume')">Resume failed phase with fresh validation</button>
    <ProjectToolchain v-if="toolchainProject" :project="toolchainProject" :editable="editable" :checkpoint="task.status + ':' + workflow?.stage" />
    <div v-if="workflow" class="notice">
      <strong v-if="task.options.long_run_quality">LONG-RUN · </strong>
      <strong v-if="workflow.cycle_number !== undefined">Cycle {{ workflow.cycle_number }} / {{ workflow.cycle_limit === null ? '∞' : workflow.cycle_limit }}</strong>
      <p>Stage: {{ workflow.stage.replaceAll('_', ' ').toUpperCase() }}</p>
      <p>Last meaningful progress: {{ workflow.last_meaningful_progress || 'None recorded' }}</p>
      <p>Files changed this cycle: {{ workflow.files_changed_this_cycle?.join(', ') || 'None' }}</p>
      <p>Reviewer: {{ workflow.latest_review?.verdict || 'Pending' }} · QA: {{ qaStatus }}</p>
      <p v-if="workflow.stop_reason">{{ workflow.stop_reason }}</p>
      <button v-if="['paused', 'stalled'].includes(task.status)" :disabled="!editable" @click="emit('resume')">Resume with fresh validation</button>
    </div>
    <button v-if="!workflow && ['paused', 'stalled'].includes(task.status)" :disabled="!editable" @click="emit('resume')">Resume with fresh validation</button>
    <details v-if="task.result?.approvals" :open="task.status === 'waiting_for_approval'">
      <summary>Approval requests</summary><pre>{{ pretty(task.result.approvals) }}</pre>
    </details>
    <p class="muted">
      {{ new Date(task.created_at).toLocaleString() }} · {{ duration }}s to last
      update · {{ task.options.project }} / {{ task.options.workspace }}
    </p>
    <button
      v-if="!terminal(task.status) || ['waiting_for_approval', 'paused', 'stalled'].includes(task.status)"
      :disabled="!editable"
      @click="emit('cancel')"
    >
      Cancel task
    </button>
    <p v-if="task.error" class="error">{{ task.error }}</p>
    <p v-if="task.status === 'interrupted'" class="notice">
      The previous runtime stopped. This task has not been resumed. Submit a new
      task to continue.
    </p>
    <div class="worker-outcomes" v-if="workers.length">
      <h3>Worker outcomes</h3>
      <div v-for="worker in workers" :key="worker.agent" class="task-row">
        <strong>{{ worker.agent }}</strong
        ><span>Execution · {{ worker.steps }} decisions</span>
        <span class="badge" :class="worker.status">{{
          worker.status.toUpperCase()
        }}</span>
      </div>
    </div>
    <section class="panel acceptance">
      <h3>Task acceptance</h3>
      <strong :class="acceptance?.status === 'accepted' ? 'green' : acceptance?.status === 'rejected' ? 'red' : 'muted'">{{
        acceptance?.status?.replaceAll("_", " ").toUpperCase() || "NOT EVALUATED"
      }}</strong>
      <p v-if="!acceptance || acceptance.status === 'not_evaluated'" class="muted">
        No explicit acceptance requirements evaluated for this task.
      </p>
      <ul>
        <li v-for="failure in acceptance?.failures || []" :key="failure">
          {{ failure }}
        </li>
      </ul>
      <details v-if="acceptance">
        <summary>Acceptance checks and evidence source</summary>
        <pre>{{ pretty(acceptance.checks) }}</pre>
      </details>
      <h3>Reviewer verdict <span class="muted">Model-reviewed</span></h3>
      <strong :class="review?.verdict === 'fail' ? 'red' : ''">{{
        review?.verdict?.toUpperCase() || "NOT RECORDED"
      }}</strong>
      <span v-if="review && !evidence?.reviewer_inspected_files?.length"> / UNVERIFIED — no complete file inspection recorded</span>
      <p>{{ review?.summary }}</p>
      <p v-if="review?.visual_inspection" class="muted">
        SOURCE INSPECTION: {{ review.source_inspection || 'not reported' }}<br />
        VISUAL INSPECTION: {{ review.visual_inspection.replaceAll('_', ' ') }}<br />
        RUNTIME ERRORS: {{ review.runtime_errors?.join('; ') || 'No model-reported errors; consult QA evidence.' }}
      </p>
      <details v-if="task.result?.visual_qa || task.result?.repair_attempts">
        <summary>Local visual QA and repair attempts</summary>
        <pre>{{ pretty({ final_qa: task.result.visual_qa, attempts: task.result.repair_attempts }) }}</pre>
      </details>
      <template v-if="review">
        <h4>Critical findings</h4>
        <ul>
          <li
            v-for="(f, i) in review.findings.filter(
              (f) => f.severity === 'critical',
            )"
            :key="i"
            class="red"
          >
            {{ f.category }}: {{ f.message }}
            <code>{{ f.affected_files.join(", ") }}</code>
          </li>
        </ul>
        <h4>Warnings and observations</h4>
        <ul>
          <li
            v-for="(f, i) in review.findings.filter(
              (f) => f.severity !== 'critical',
            )"
            :key="i"
          >
            {{ f.severity }} · {{ f.message }}
            <code>{{ f.affected_files.join(", ") }}</code>
          </li>
        </ul>
        <p class="muted">
          Findings and cited event IDs are reviewer claims. File-read evidence
          proves inspection coverage, not the correctness of a finding.
        </p>
      </template>
    </section>
    <div class="detail-grid">
      <div>
        <h3>Agent summary <span class="muted">Unverified claims</span></h3>
        <details><summary>Show agent summary</summary>
          <pre>{{ task.result?.summary || "Awaiting agent result…" }}</pre>
        </details>
        <h3>Supervisor plan</h3>
        <pre>{{ task.result?.plan || "No supervisor plan recorded." }}</pre>
        <details>
          <summary>Workers and models used</summary>
          <pre>{{
            pretty({
              workers: task.result?.workers,
              models: task.result?.models,
              results: task.result?.worker_results,
            })
          }}</pre>
        </details>
      </div>
      <div>
        <h3>Verified execution evidence</h3>
        <p class="muted">{{ evidence?.scope }}</p>
        <h4>Files created / modified</h4>
        <ul v-if="evidence?.files_modified.length">
          <li v-for="file in evidence.files_modified" :key="file">
            <code>{{ file }}</code>
          </li>
        </ul>
        <p v-else class="muted">No successful file writes recorded.</p>
        <h4>Verified after latest modification</h4>
        <ul>
          <li v-for="file in evidence?.files_modified || []" :key="file">
            {{ evidence?.verified_after_write?.includes(file) ? "✓" : "✗" }}
            <code>{{ file }}</code>
            {{
              evidence?.verified_after_write?.includes(file)
                ? "— complete read after latest write"
                : "— complete readback missing"
            }}
          </li>
        </ul>
        <h4>Complete reviewer inspection</h4>
        <pre>{{ pretty(evidence?.reviewer_inspected_files || []) }}</pre>
        <h4>Tests actually executed</h4>
        <pre>{{ pretty(evidence?.tests_executed || []) }}</pre>
        <details>
          <summary>Verification actions and tool results</summary>
          <pre>{{ pretty(evidence?.tool_calls || []) }}</pre>
        </details>
        <h4>Approvals</h4>
        <pre>{{
          pretty(
            task.result?.pending_approvals ||
              events
                .filter((e) => e.type === "privileged_action_requested")
                .map((e) => e.payload),
          )
        }}</pre>
        <p class="muted">
          Privileged approval uses the separate operator CLI. Active workflows
          continue with the operator's result; actions are not automatically retried.
        </p>
      </div>
    </div>
    <section v-if="timeoutHistory.length">
      <h3>Timeout recovery</h3>
      <p v-for="(item, index) in timeoutHistory" :key="index" class="yellow">
        {{ item.agent }} · attempt {{ item.attempt }} ·
        {{ item.seconds ?? "unknown" }}s timeout · {{ item.result }}
      </p>
    </section>
    <details v-if="warnings.length" open>
      <summary>
        Warnings, invalid responses and retries · {{ warnings.length }}
      </summary>
      <pre>{{
        pretty(
          warnings.map((e) => ({ type: e.type, agent: e.actor, ...e.payload })),
        )
      }}</pre>
    </details>
    <div class="section-top">
      <h3>Live execution</h3>
      <span class="muted">Existing audit stream · polls every 2s</span>
    </div>
    <div class="events">
      <div v-for="event in events" :key="event.id" class="event">
        <time>{{ new Date(event.timestamp).toLocaleTimeString() }}</time
        ><span><strong v-if="event.payload.phase_id" class="phase-badge">[P{{ event.payload.phase_index }} {{ event.payload.phase_title }} · attempt {{ event.payload.phase_attempt }}]</strong> {{ event.actor || "runtime" }}</span>
        <details>
          <summary
            :class="{
              red:
                event.type.includes('denied') || event.type.includes('failed'),
              blue: event.type === 'model_request',
            }"
          >
            {{ event.type.replaceAll("_", " ") }}
            <code>{{ event.payload.tool || event.payload.model || "" }}</code>
          </summary>
          <pre>{{ pretty(event.payload) }}</pre>
        </details>
      </div>
      <p v-if="!events.length" class="muted">Waiting for execution events.</p>
    </div>
    <details>
      <summary>
        Development prompt inspection ·
        {{ inspection ? "enabled, session only" : "disabled" }}
      </summary>
      <p class="muted">
        Default audit storage contains prompt metadata and hashes only.
        Inspection is operator-only and loopback-only; the session cache is
        bounded and cleared on shutdown.
      </p>
      <button v-if="inspection && editable" @click="loadPrompts">
        Inspect session prompts
      </button>
      <p class="error">{{ promptError }}</p>
      <pre v-if="prompts">{{ pretty(prompts) }}</pre>
    </details>
  </section>
</template>
