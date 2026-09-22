<script setup lang="ts">
import { ref } from "vue";
const props = defineProps<{
  project: { name: string; workspace: string };
  profiles: string[];
  editable: boolean;
  busy: boolean;
}>();
const emit = defineEmits<{ run: [body: Record<string, unknown>] }>();
const goal = ref(""),
  agent = ref("auto"),
  maxSteps = ref<number | "">(""),
  profile = ref("");
const requiredFiles = ref(""),
  requireReadback = ref(false),
  requireReview = ref(false);
const visualProject = ref(""), repairCycles = ref(2);
const stepsMode = ref("profile"), repairMode = ref("default"), longRun = ref(false);
const paths = () =>
  requiredFiles.value
    .split(/\r?\n/)
    .map((p) => p.trim())
    .filter(Boolean);
function run() {
  emit("run", {
    goal: goal.value,
    agent: visualProject.value.trim() ? "auto" : agent.value,
    visual_project: visualProject.value.trim() || null,
    max_repair_cycles: repairMode.value === "unlimited" ? null : repairMode.value === "default" ? 2 : repairCycles.value,
    worker_steps_mode: stepsMode.value,
    long_run_quality: longRun.value,
    project: props.project.name,
    workspace: props.project.workspace,
    max_steps: stepsMode.value === "custom" && maxSteps.value !== "" ? maxSteps.value : null,
    model_profile: profile.value || null,
    acceptance: {
      required_files: paths(),
      required_read_after_write: requireReadback.value ? paths() : [],
      require_readback_all_modified: requireReadback.value,
      required_reviewer_files: requireReview.value ? paths() : [],
      required_review_verdict: requireReview.value ? "pass" : null,
      required_workers: requireReview.value ? ["reviewer"] : [],
    },
  });
}
</script>
<template>
  <section class="panel">
    <div class="section-top">
      <h2>Run a task</h2>
      <span class="eyebrow">WORKSPACE CONFINED</span>
    </div>
    <form @submit.prevent="run">
      <label for="goal">Ask the runtime to…</label
      ><textarea
        id="goal"
        v-model="goal"
        rows="4"
        maxlength="4000"
        required
        placeholder="Describe a concrete goal, files to inspect, and what should be verified."
      />
      <div class="form-row">
        <label
          >Routing<select v-model="agent">
            <option value="auto">AUTO · Supervisor flow</option>
            <option
              v-for="role in ['supervisor', 'researcher', 'coder', 'reviewer']"
              :key="role"
              :value="role"
            >
              {{ role }}
            </option>
          </select></label
        ><button class="primary" :disabled="!editable || busy || !goal.trim()">
          {{ busy ? "Creating…" : "Run task →" }}
        </button>
      </div>
      <p v-if="!['auto', 'supervisor'].includes(agent)" class="muted">
        Direct worker routing skips Supervisor planning. Broker and policy
        checks still apply.
      </p>
      <details>
        <summary>Advanced options</summary>
        <label>Local visual QA project (workspace-relative, optional)
          <input v-model="visualProject" placeholder="my-site" />
        </label>
        <label><input type="checkbox" v-model="longRun" /> Long-Run Quality Mode</label>
        <p v-if="longRun" class="muted">Quality before speed. Fresh repair invocations, verified acceptance and one final polish pass. Unlimited is opt-in below; cancellation remains available.</p>
        <label v-if="visualProject.trim()">Visual refinement
          <select v-model="repairMode" aria-label="Visual refinement">
            <option value="default">Default (2)</option><option value="custom">Custom</option>
            <option value="unlimited">Unlimited</option>
          </select>
          <input v-if="repairMode === 'custom'" v-model.number="repairCycles" type="number" min="1" required aria-label="Custom repair cycles" />
        </label>
        <p v-if="visualProject.trim()" class="muted">Uses Coder → local QA → Reviewer. Only the final review determines acceptance.</p>
        <p class="muted">{{ project.name }} · {{ project.workspace }}</p>
        <label
          >Required file paths (one relative path per line)<textarea
            v-model="requiredFiles"
            rows="3"
          />
        </label>
        <label
          ><input type="checkbox" v-model="requireReadback" /> Require complete
          readback after latest write</label
        >
        <label
          ><input type="checkbox" v-model="requireReview" /> Require reviewer
          PASS and complete inspection of listed files</label
        >
        <p class="muted">
          These explicit checks enforce evidence. Natural-language requests
          alone are not automatically converted into acceptance rules.
        </p>
        <div class="form-row">
          <label
            >Worker steps<select v-model="stepsMode" aria-label="Worker steps">
              <option value="profile">Profile default</option><option value="custom">Custom</option>
              <option value="unlimited">Unlimited</option>
            </select><input v-if="stepsMode === 'custom'"
              type="number"
              v-model.number="maxSteps"
              min="1"
              required aria-label="Custom worker steps" /></label
          ><label
            >Explicit model override<select v-model="profile">
              <option value="">Use each agent’s profile</option>
              <option v-for="p in profiles" :key="p">{{ p }}</option>
            </select></label
          >
        </div>
      </details>
    </form>
  </section>
</template>
