// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import TaskRunner from "./TaskRunner.vue";
import TaskDetail from "./TaskDetail.vue";
import BrowserReadiness from "./BrowserReadiness.vue";
import AgentInspector from "./AgentInspector.vue";
import ModelSettings from "./ModelSettings.vue";
import { reactive } from "vue";
import { api, clone, terminal } from "../api";
import type { Agent, Configuration, Task } from "../types";

describe("operator control surface", () => {
  it("requires a real launch test to show browser READY", async () => {
    const w = mount(BrowserReadiness, { props: {
      editable: true, browser: { status: "READY", error: null, launch_test: false },
    }});
    expect(w.get(".badge").text()).toBe("UNAVAILABLE");
    await w.setProps({ browser: { status: "READY", error: null, launch_test: true } });
    expect(w.get(".badge").text()).toBe("READY");
    await w.get("button").trigger("click");
    expect(w.emitted("check")).toHaveLength(1);
  });
  it("uses a short task title and keeps instructions collapsed", () => {
    const task: Task = {
      id: "test-id", goal: "A very long prompt\n".repeat(80), status: "waiting_for_approval",
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      result: { approvals: [{ request_id: "request-123", status: "awaiting_approval" }] },
      error: null, options: { project: "default", workspace: "/safe", agent: "auto",
                             visual_project: "studio-26/" },
    };
    const w = mount(TaskDetail, { props: {
      task, events: [], evidence: null, editable: true, inspection: false,
    }});
    expect(w.get("h2").text()).toBe("Task studio-26");
    const instructions = w.findAll("details").find(x => x.get("summary").text() === "Task instructions")!;
    expect(instructions.attributes("open")).toBeUndefined();
    expect(instructions.text()).toContain("A very long prompt");
    expect(w.text()).toContain("request-123");
    expect(w.text()).toContain("awaiting_approval");
  });
  it("sends routing and confined project information", async () => {
    const w = mount(TaskRunner, {
      props: {
        project: { name: "demo", workspace: "/safe" },
        profiles: ["fast"],
        editable: true,
        busy: false,
      },
    });
    await w.get("textarea").setValue("Review the project");
    await w.get("form").trigger("submit");
    expect(w.emitted("run")?.[0]?.[0]).toMatchObject({
      goal: "Review the project",
      agent: "auto",
      project: "demo",
      workspace: "/safe",
      model_profile: null,
      max_steps: null,
    });
  });
  it("viewers cannot submit tasks", () => {
    const w = mount(TaskRunner, {
      props: {
        project: { name: "demo", workspace: "/safe" },
        profiles: [],
        editable: false,
        busy: false,
      },
    });
    expect(w.get("button").attributes("disabled")).toBeDefined();
  });
  it("separates unsupported claims from recorded evidence", () => {
    const task: Task = {
      id: "1",
      goal: "review",
      status: "completed",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      result: { summary: "All tests passed" },
      error: null,
      options: { project: "demo", workspace: "/safe", agent: "coder" },
    };
    const w = mount(TaskDetail, {
      props: {
        task,
        events: [],
        evidence: {
          tool_calls: [],
          files_modified: [],
          tests_executed: [],
          verification_actions: [],
          scope: "Recorded tool execution only",
        },
        editable: true,
        inspection: false,
      },
    });
    expect(w.text()).toContain("Unverified claims");
    expect(w.text()).toContain("All tests passed");
    expect(w.text()).toContain("No successful file writes recorded");
    expect(w.text()).toContain("disabled");
  });
  it("only the mandate has an editor", async () => {
    const agent: Agent = {
      id: "coder",
      name: "coder",
      role: "coder",
      profile: "fast",
      provider: "local",
      model: "qwen",
      location: "local",
      endpoint: "http://localhost",
      max_tokens: 8192,
      tools: ["filesystem.write"],
      risk_limit: "high",
      state: "idle",
      current_task: null,
      connection: "not_tested",
      mandate: "Implement changes",
      security_rules: "No shell",
      system_prompt: "Actual prompt",
    };
    const w = mount(AgentInspector, { props: { agent, editable: true } });
    expect(w.findAll("textarea")).toHaveLength(1);
    await w.get("textarea").setValue("Read before editing");
    await w.findAll("button")[1]!.trigger("click");
    expect(w.emitted("save")?.[0]).toEqual(["Read before editing"]);
    expect(w.text()).toContain("Read-only");
  });
  it("edits model assignments from reactive backend data", async () => {
    const configuration: Configuration = {
      profiles: {
        fast: {
          provider: "scripted",
          model: "scripted",
          base_url: "http://localhost:11434/v1",
          api_key_env: null,
          max_tokens: 1024,
          temperature: 0,
          timeout_seconds: 30,
          retry_count: 1,
          structured_output: "schema",
          local_server: "ollama",
          credential_configured: false,
        },
      },
      agents: { coder: { profile: "fast", mandate: null } },
    };
    const w = mount(ModelSettings, {
      props: { configuration: reactive(configuration), editable: true },
    });
    const save = w
      .findAll("button")
      .find((b) => b.text() === "Save configuration")!;
    await save.trigger("click");
    const body = w.emitted("save")?.[0]?.[0] as Configuration;
    expect(body.profiles.fast?.model).toBe("scripted");
    expect(body.profiles.fast).not.toHaveProperty("credential_configured");
    expect(clone(reactive(configuration))).toEqual(configuration);
  });
  it("recognizes parked and terminal tasks", () => {
    expect(terminal("waiting_for_approval")).toBe(true);
    expect(terminal("running")).toBe(false);
    expect(terminal("interrupted")).toBe(true);
  });
  it("shows exhausted workers separately and explains interruptions", () => {
    const task: Task = {
      id: "interrupted",
      goal: "inspect",
      status: "interrupted",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      result: {
        worker_results: [{ agent: "coder", status: "exhausted", steps: 16 }],
      },
      error: "Runtime restarted before task completion.",
      options: { project: "demo", workspace: "/safe", agent: "auto" },
    };
    const w = mount(TaskDetail, {
      props: {
        task,
        events: [],
        evidence: null,
        editable: true,
        inspection: false,
      },
    });
    expect(w.text()).toContain("EXHAUSTED");
    expect(w.text()).toContain("16 decisions");
    expect(w.text()).toContain("has not been resumed");
    expect(w.findAll("button").some((b) => b.text() === "Cancel task")).toBe(
      false,
    );
  });
  it.each(["completed", "failed", "interrupted"])(
    "renders %s tasks with distinct review and acceptance",
    (status) => {
      const task: Task = {
        id: "t",
        goal: "review",
        status,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        error: null,
        options: { project: "demo", workspace: "/safe", agent: "auto" },
        result: {
          worker_results: [
            { agent: "reviewer", status: "completed", steps: 2 },
          ],
          review: {
            verdict: "fail",
            summary: "Incomplete implementation",
            findings: [
              {
                severity: "critical",
                category: "logic",
                message: "Missing transaction calculation",
                affected_files: ["app.js"],
                evidence_event_ids: [],
              },
            ],
            acceptance_criteria: [],
          },
          acceptance: {
            status: "failed",
            failures: ["critical review finding"],
            checks: [],
          },
        },
      };
      const w = mount(TaskDetail, {
        props: {
          task,
          events: [],
          evidence: null,
          editable: true,
          inspection: false,
        },
      });
      expect(w.text()).toContain("COMPLETED");
      expect(w.text()).toContain("Reviewer verdict");
      expect(w.text()).toContain("FAILED");
      expect(w.text()).toContain("Critical findings");
      expect(w.text()).toContain("Missing transaction calculation");
      expect(w.text()).toContain("Model-reviewed");
    },
  );
  it("identifies the failing HTTP endpoint without disclosing credentials", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }),
    );
    try {
      await expect(api("/tasks/example/evidence")).rejects.toThrow(
        "500): GET /tasks/example/evidence",
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it.each(["not_evaluated", "accepted", "rejected"])("renders acceptance %s and unverified PASS", (status) => {
    const task: Task = {
      id: "acceptance", goal: "review", status: "completed",
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(), error: null,
      options: { project: "demo", workspace: "/safe", agent: "auto" },
      result: {
        acceptance: { status, failures: [], checks: [], review_inspection: "unverified" },
        review: { verdict: "pass", summary: "Model claim", findings: [], acceptance_criteria: [] },
      },
    };
    const w = mount(TaskDetail, { props: { task, events: [], evidence: null, editable: false, inspection: false } });
    expect(w.text()).toContain(status.replaceAll("_", " ").toUpperCase());
    expect(w.text()).toContain("UNVERIFIED");
    if (status === "not_evaluated") expect(w.text()).not.toContain("ACCEPTED");
  });

  it("selects the bounded local visual QA workflow", async () => {
    const w = mount(TaskRunner, {props: {project: {name: "demo", workspace: "/safe"}, profiles: [], editable: true, busy: false}});
    await w.get("textarea").setValue("Repair site");
    await w.get('input[placeholder="my-site"]').setValue("site");
    await w.get("form").trigger("submit");
    expect(w.emitted("run")?.[0]?.[0]).toMatchObject({visual_project: "site", max_repair_cycles: 2, agent: "auto"});
    expect(w.text()).toContain("Only the final review determines acceptance");
  });

  it("requires explicit unlimited selection and accepts custom limits above 50", async () => {
    const w = mount(TaskRunner, {props: {project: {name: "demo", workspace: "/safe"}, profiles: [], editable: true, busy: false}});
    await w.get("textarea").setValue("Improve page");
    await w.get('input[placeholder="my-site"]').setValue("site");
    await w.get('select[aria-label="Worker steps"]').setValue("custom");
    await w.get('input[aria-label="Custom worker steps"]').setValue(120);
    await w.get("form").trigger("submit");
    expect(w.emitted("run")?.[0]?.[0]).toMatchObject({max_steps: 120, worker_steps_mode: "custom", max_repair_cycles: 2, long_run_quality: false});
    await w.get('select[aria-label="Worker steps"]').setValue("unlimited");
    await w.get('select[aria-label="Visual refinement"]').setValue("unlimited");
    await w.get("form").trigger("submit");
    expect(w.emitted("run")?.[1]?.[0]).toMatchObject({max_steps: null, worker_steps_mode: "unlimited", max_repair_cycles: null});
  });

  it("shows infinity, live progress, cancel and operator resume", async () => {
    const task: Task = {
      id: "long", goal: "Improve", status: "paused", error: null,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      options: {project: "demo", workspace: "/safe", agent: "auto", long_run_quality: true, visual_project: "site"},
      result: {workflow: {cycle_number: 7, cycle_limit: null, stage: "coder_repair", files_changed_this_cycle: ["site/index.html"], latest_review: {verdict: "fail"}}},
    };
    const w = mount(TaskDetail, {props: {task, events: [], evidence: null, editable: true, inspection: false}});
    expect(w.text()).toContain("Cycle 7 / ∞");
    expect(w.text()).toContain("LONG-RUN");
    expect(w.text()).toContain("CODER REPAIR");
    expect(w.text()).toContain("site/index.html");
    await w.findAll("button").find(b => b.text() === "Cancel task")!.trigger("click");
    await w.findAll("button").find(b => b.text().startsWith("Resume"))!.trigger("click");
    expect(w.emitted("cancel")).toHaveLength(1);
    expect(w.emitted("resume")).toHaveLength(1);
  });

});
