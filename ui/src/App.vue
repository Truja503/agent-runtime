<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import { clone, api, setToken, taskTitle } from "./api";
import type {
  Configuration,
  Evidence,
  Event,
  RuntimeState,
  Task,
  WebSearchConfiguration,
} from "./types";
import RuntimeGraph from "./components/RuntimeGraph.vue";
import AgentInspector from "./components/AgentInspector.vue";
import TaskRunner from "./components/TaskRunner.vue";
import ModelSettings from "./components/ModelSettings.vue";
import TaskDetail from "./components/TaskDetail.vue";
import BrowserReadiness from "./components/BrowserReadiness.vue";
const token = ref(""),
  connected = ref(false),
  role = ref("viewer"),
  view = ref("overview"),
  error = ref(""),
  notice = ref(""),
  loading = ref(false);
const state = ref<RuntimeState | null>(null),
  configuration = ref<Configuration | null>(null),
  webConfiguration = ref<WebSearchConfiguration | null>(null),
  webTest = ref<Record<string, unknown> | null>(null),
  tasks = ref<Task[]>([]),
  selectedId = ref(""),
  selectedNode = ref(""),
  taskEvents = ref<Event[]>([]),
  evidence = ref<Evidence | null>(null),
  health = ref<Record<string, string>>({});
const selectedTask = computed(() =>
    tasks.value.find((t) => t.id === selectedId.value),
  ),
  selectedAgent = computed(() =>
    state.value?.agents.find((a) => a.id === selectedNode.value),
  ),
  node = computed(() =>
    state.value?.graph.nodes.find((n) => n.id === selectedNode.value),
  ),
  editable = computed(() => role.value === "operator");
let timer: ReturnType<typeof setTimeout> | undefined,
  refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [runtime, list] = await Promise.all([
      api<RuntimeState>("/runtime"),
      api<Task[]>("/tasks?limit=100"),
    ]);
    state.value = runtime;
    tasks.value = list;
    if (selectedId.value) {
      const id = selectedId.value;
      const [events, facts] = await Promise.all([
        api<Event[]>(`/tasks/${id}/events`),
        api<Evidence>(`/tasks/${id}/evidence`),
      ]);
      if (id === selectedId.value) {
        taskEvents.value = events;
        evidence.value = facts;
      }
    }
  } catch (e) {
    error.value = String(e);
  } finally {
    refreshing = false;
  }
}
async function poll() {
  await refresh();
  if (connected.value) timer = setTimeout(poll, 2000);
}
async function checkBrowser() {
  try {
    const ready = await api<NonNullable<RuntimeState["browser"]>>("/browser/readiness", "POST");
    if (state.value) state.value.browser = ready;
  } catch (e) { error.value = String(e); }
}
async function connect() {
  loading.value = true;
  error.value = "";
  setToken(token.value);
  try {
    const me = await api<{ role: string }>("/auth/whoami");
    role.value = me.role;
    configuration.value = await api<Configuration>("/models");
    webConfiguration.value = await api<WebSearchConfiguration>("/web/config");
    connected.value = true;
    token.value = "";
    await poll();
  } catch (e) {
    error.value = String(e);
  } finally {
    loading.value = false;
  }
}
function disconnect() {
  connected.value = false;
  clearTimeout(timer);
  setToken("");
  state.value = null;
  tasks.value = [];
  configuration.value = null;
  webConfiguration.value = null;
  webTest.value = null;
  selectedId.value = "";
  selectedNode.value = "";
  taskEvents.value = [];
  evidence.value = null;
}
async function saveWebConfiguration() {
  if (!webConfiguration.value) return;
  error.value = "";
  notice.value = "";
  try {
    webConfiguration.value = await api<WebSearchConfiguration>("/web/config", "PUT", {
      provider: webConfiguration.value.provider,
      searxng_base_url: webConfiguration.value.searxng_base_url,
    });
    webTest.value = null;
    await refresh();
    notice.value = "Web search configuration saved.";
  } catch (e) {
    error.value = String(e);
  }
}
async function testWebSearch() {
  error.value = "";
  try {
    webTest.value = await api<Record<string, unknown>>("/web/search/test", "POST");
    await refresh();
  } catch (e) {
    error.value = String(e);
  }
}
async function run(body: Record<string, unknown>) {
  loading.value = true;
  error.value = "";
  try {
    const task = await api<Task>("/tasks", "POST", body);
    selectedId.value = task.id;
    view.value = "tasks";
    await refresh();
  } catch (e) {
    error.value = String(e);
  } finally {
    loading.value = false;
  }
}
async function save(body: Configuration) {
  error.value = "";
  notice.value = "";
  try {
    configuration.value = await api<Configuration>("/models", "PUT", body);
    health.value = {};
    await refresh();
    notice.value = "Configuration saved. New tasks use these profiles.";
  } catch (e) {
    error.value = String(e);
  }
}
async function saveMandate(mandate: string) {
  if (!configuration.value || !selectedAgent.value) return;
  const draft: Configuration = clone({
    profiles: configuration.value.profiles,
    agents: configuration.value.agents,
  });
  for (const p of Object.values(draft.profiles)) {
    delete p.credential_configured;
    delete p.location;
  }
  draft.agents[selectedAgent.value.id]!.mandate = mandate;
  await save(draft);
}
async function cancel() {
  try {
    await api(`/tasks/${selectedId.value}/cancel`, "POST");
    await refresh();
  } catch (e) {
    error.value = String(e);
  }
}
async function resume() {
  try { await api(`/tasks/${selectedId.value}/resume`, "POST"); await refresh(); }
  catch (e) { error.value = String(e); }
}
async function selectTask(id: string) {
  selectedId.value = id;
  taskEvents.value = [];
  evidence.value = null;
  await refresh();
}
onUnmounted(() => clearTimeout(timer));
</script>
<template>
  <div class="console">
    <aside class="sidebar">
      <a class="brand" href="#" @click.prevent="view = 'overview'"
        ><span class="brand-icon">⌘</span
        ><span>AGENT<br /><strong>RUNTIME</strong></span></a
      >
      <p class="eyebrow">CONTROL CENTER</p>
      <nav>
        <button
          :class="{ selected: view === 'overview' }"
          @click="view = 'overview'"
        >
          ◈ &nbsp; Overview</button
        ><button
          :class="{ selected: view === 'tasks' }"
          @click="view = 'tasks'"
        >
          ≡ &nbsp; Task execution <span>{{ tasks.length }}</span></button
        ><button
          :class="{ selected: view === 'models' }"
          @click="view = 'models'"
        >
          ⊞ &nbsp; Models & settings
        </button>
      </nav>
      <div class="sidebar-bottom">
        <span class="status-dot" :class="{ online: connected }"></span
        >{{ connected ? "API connected" : "Disconnected" }}
        <p class="muted">Intelligence ≠ authority</p>
        <button v-if="connected" @click="disconnect">Disconnect</button>
      </div>
    </aside>
    <main>
      <header>
        <div>
          <p class="eyebrow">LOCAL FIRST / OPERATOR CONSOLE</p>
          <h1>
            {{
              view === "overview"
                ? "Runtime overview"
                : view === "tasks"
                  ? "Task execution"
                  : "Models & settings"
            }}
          </h1>
        </div>
        <div class="header-meta">
          <span class="badge">{{
            connected ? role : "AUTHENTICATION REQUIRED"
          }}</span>
          <p v-if="state" class="muted">
            {{ state.project.name }} · {{ state.queued_tasks }} queued / running
          </p>
        </div>
      </header>
      <div v-if="error" class="error" role="alert">
        {{ error }}
        <button @click="error = ''" aria-label="Dismiss error">×</button>
      </div>
      <p v-if="notice" class="notice" role="status">
        {{ notice }}
        <button @click="notice = ''" aria-label="Dismiss notice">×</button>
      </p>
      <section v-if="!connected" class="panel login">
        <p class="eyebrow">CONNECT TO YOUR RUNTIME</p>
        <h2>One place to inspect every decision.</h2>
        <p class="muted">
          Use an API bearer token configured on this backend. The token stays in
          memory for this session.
        </p>
        <form @submit.prevent="connect">
          <label
            >API token<input
              v-model="token"
              type="password"
              autocomplete="off"
              required
              placeholder="Enter API token" /></label
          ><button class="primary" :disabled="loading">
            {{ loading ? "Connecting…" : "Connect runtime →" }}
          </button>
        </form>
      </section>
      <template v-if="connected && state && configuration"
        ><div class="workspace-bar">
          <span class="eyebrow">PROJECT</span
          ><strong>{{ state.project.name }}</strong
          ><code>{{ state.project.workspace }}</code
          ><span class="muted">Confined workspace</span>
        </div>
        <BrowserReadiness :browser="state.browser" :editable="editable" @check="checkBrowser" />
        <template v-if="view === 'overview'"
          ><div class="agent-grid">
            <button
              class="agent-card"
              v-for="agent in state.agents"
              :key="agent.id"
              @click="selectedNode = agent.id"
            >
              <div class="section-top">
                <span class="eyebrow">{{ agent.role }}</span
                ><span
                  class="status-dot"
                  :class="{ online: !!agent.current_task }"
                ></span>
              </div>
              <h2>{{ agent.name }}</h2>
              <p class="model-name">{{ agent.model }}</p>
              <p class="muted">{{ agent.provider }} / {{ agent.location }}</p>
              <div class="card-foot">
                <span
                  >{{ agent.max_tokens.toLocaleString() }} max output ·
                  {{ agent.max_steps === null ? '∞' : agent.max_steps }} steps</span
                ><span class="badge">{{
                  agent.role === "web" ? agent.state : agent.current_task ? "running" : "idle"
                }}</span>
              </div>
              <p class="muted">
                {{
                  health[agent.profile] || agent.connection.replaceAll("_", " ")
                }}
                · {{ agent.tools.length }} tools
              </p>
              <div class="chips">
                <code v-for="tool in agent.tools" :key="tool">{{ tool }}</code
                ><code v-if="!agent.tools.length">Coordination only</code>
              </div>
            </button>
          </div>
          <section v-if="state.internet" class="panel">
            <div class="section-top">
              <div>
                <h3>Internet · {{ state.internet.enabled ? "enabled" : "disabled" }}</h3>
                <p>Search provider: {{ state.internet.search_provider_configured ? state.internet.search_provider : "not configured" }} · Recent denied requests: {{ state.internet.recent_denied.length }}</p>
              </div>
              <span class="badge">{{ state.internet.search_provider_configured ? "SEARCH READY" : "SEARCH OFF" }}</span>
            </div>
            <p class="muted">Kill switch: INTERNET_ACCESS_ENABLED={{ state.internet.enabled ? "true" : "false" }} (server configuration)</p>
            <div v-if="webConfiguration" class="form-row">
              <label>Search provider
                <select v-model="webConfiguration.provider" :disabled="!editable">
                  <option value="none">None</option>
                  <option value="searxng">SearXNG</option>
                </select>
              </label>
              <label v-if="webConfiguration.provider === 'searxng'">SearXNG endpoint
                <input v-model="webConfiguration.searxng_base_url" :disabled="!editable" placeholder="http://127.0.0.1:8080" />
              </label>
            </div>
            <div v-if="webConfiguration" class="actions">
              <button :disabled="!editable" @click="saveWebConfiguration">Save search</button>
              <button :disabled="!editable || !state.internet.enabled || webConfiguration.provider === 'none'" @click="testWebSearch">Test search</button>
            </div>
            <p v-if="webTest" class="muted">Search test: {{ webTest.status }} · {{ webTest.result_count ?? 0 }} results</p>
            <p class="muted">SearXNG configuration is persisted in data/web-search.json; no search query receives the task prompt or workspace files.</p>
            <details><summary>Recent web requests</summary>
              <p v-for="(request, i) in state.internet.recent_requests" :key="i">
                {{ request.operation }} · {{ request.status }} · External data sent:
                {{ request.external_data_sent.join("; ") || "None" }}
              </p>
            </details>
          </section>
          <RuntimeGraph :graph="state.graph" @select="selectedNode = $event" />
          <div class="overview-bottom">
            <TaskRunner
              :project="state.project"
              :profiles="Object.keys(configuration.profiles)"
              :editable="editable"
              :busy="loading"
              @run="run"
            />
            <section class="panel">
              <div class="section-top">
                <h2>Recent activity</h2>
                <button @click="view = 'tasks'">View all →</button>
              </div>
              <button
                class="task-row"
                v-for="task in tasks.slice(0, 5)"
                :key="task.id"
                @click="
                  view = 'tasks';
                  selectTask(task.id);
                "
              >
                <span>{{ taskTitle(task) }}</span
                ><span class="badge">{{ task.status }}</span>
              </button>
              <p v-if="!tasks.length" class="empty">
                No tasks yet. Run a goal to see the execution trail.
              </p>
            </section>
          </div></template
        ><template v-else-if="view === 'tasks'"
          ><TaskRunner
            :project="state.project"
            :profiles="Object.keys(configuration.profiles)"
            :editable="editable"
            :busy="loading"
            @run="run"
          />
          <div class="task-layout">
            <section class="panel task-list">
              <h2>Tasks</h2>
              <button
                v-for="task in tasks"
                :key="task.id"
                class="task-row"
                :class="{ selected: task.id === selectedId }"
                @click="selectTask(task.id)"
              >
                <span>{{ taskTitle(task) }}</span
                ><small>{{ task.status }} · {{ task.id.slice(0, 8) }}</small>
              </button>
              <p v-if="!tasks.length" class="empty">No recorded tasks.</p>
            </section>
            <TaskDetail
              v-if="selectedTask"
              :key="selectedTask.id"
              :task="selectedTask"
              :events="taskEvents"
              :evidence="evidence"
              :editable="editable"
              :inspection="state.prompt_inspection"
              @cancel="cancel"
              @resume="resume"
            />
            <section v-else class="panel empty">
              Select a task to inspect its claims, evidence and event stream.
            </section>
          </div></template
        ><ModelSettings
          v-else
          :configuration="configuration"
          :editable="editable"
          @save="save"
          @error="error = $event"
          @health="(p, s) => (health[p] = s)"
        /><AgentInspector
          v-if="selectedAgent"
          :key="selectedAgent.id"
          :agent="selectedAgent"
          :editable="editable"
          @close="selectedNode = ''"
          @save="saveMandate"
        />
        <aside v-else-if="node" class="inspector panel">
          <div class="section-top">
            <h2>{{ node.label }}</h2>
            <button @click="selectedNode = ''">Close</button>
          </div>
          <p>{{ node.kind }} · {{ node.state }}</p>
          <p class="muted">Connections reported by the running backend:</p>
          <pre>{{
            state.graph.edges.filter(
              (e) => e.source === node?.id || e.target === node?.id,
            )
          }}</pre>
        </aside></template
      >
      <footer>
        AGENT RUNTIME
        <span
          >Model output is untrusted. Every capability passes through
          policy.</span
        >
      </footer>
    </main>
  </div>
</template>
