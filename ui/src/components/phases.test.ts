// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import PhaseExecutionPanel from "./PhaseExecution.vue";
import TaskDetail from "./TaskDetail.vue";
import type { PhaseExecution, Task } from "../types";

const execution: PhaseExecution = {
  plan: { summary: "Build Pocket Ledger", phases: ["environment", "backend", "frontend"].map(id => ({
    id, title: id, goal: `Implement ${id}`, workers: ["coder"], depends_on: [],
    requirements: { required_files: ["app.py"] }, verification: ["test"], workflow: "workers",
  })) }, current_phase_index: 1, status: "paused",
  phases: ["passed", "paused", "pending"].map((status, index) => ({
    id: ["environment", "backend", "frontend"][index], status, attempt: index === 1 ? 2 : 1,
    active_worker: "coder", outstanding: index === 1 ? ["project.test failed"] : [],
    files_changed: ["app.py"], last_meaningful_progress: "2026-09-25T12:00:00Z",
    pending_approvals: [], stop_reason: index === 1 ? "runtime_restart" : null,
    evidence: { verified_files: ["app.py"] },
  })),
};
describe("durable phase execution", () => {
  it("shows completed, current and pending phases with missing evidence", () => {
    const panel = mount(PhaseExecutionPanel, { props: { execution, taskStatus: "paused" } });
    expect(panel.text()).toContain("PAUSED IN PHASE 2/3 · BACKEND");
    expect(panel.text()).toContain("project.test failed");
    expect(panel.text()).toContain("2 attempt(s)");
    expect(panel.text()).toContain("PASSED");
    expect(panel.text()).toContain("PENDING");
  });
  it("labels live events by phase and keeps legacy tasks renderable", async () => {
    const task: Task = { id: "task", goal: "large instructions", status: "paused",
      created_at: "2026-09-25", updated_at: "2026-09-25", error: null,
      options: {agent: "auto", project: "default", workspace: "workspace"},
      result: { phase_execution: execution } };
    const panel = mount(TaskDetail, { props: { task, editable: true, evidence: null,
      inspection: false, events: [{id: "event", type: "tool_requested", actor: "coder",
        timestamp: "2026-09-25", payload: { phase_id: "backend", phase_title: "Backend",
          phase_index: 2, phase_attempt: 2, tool: "project.test" }}] } });
    expect(panel.text()).toContain("[P2 Backend · attempt 2]");
    expect(panel.text()).toContain("Resume with fresh validation");
    await panel.setProps({ task: {...task, result: null} });
    expect(panel.find(".phase-execution").exists()).toBe(false);
    expect(panel.text()).toContain("Task instructions");
  });
});
