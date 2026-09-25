// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import ProjectToolchain from "./ProjectToolchain.vue";
import { api } from "../api";

vi.mock("../api", () => ({ api: vi.fn(), pretty: JSON.stringify }));
const request = { request_id: "dep-1", project: "site", status: "awaiting_approval",
  manifest: { python: { flask: "3.1.2" } }, additional_python: [], additional_npm: [] };

describe("project toolchain approval", () => {
  beforeEach(() => {
    vi.mocked(api).mockReset();
    vi.mocked(api).mockImplementation(async (path) => path.endsWith("/inspect")
      ? { framework: "flask", environment: "NOT READY", server: "stopped" }
      : path.endsWith("/approvals") ? [request] : { status: "executed" });
  });
  it("requires separate credentials and submits only explicit package approval", async () => {
    const wrapper = mount(ProjectToolchain, { props: { project: "site", editable: true } });
    await flushPromises();
    const approve = wrapper.findAll("button").find(b => b.text().startsWith("Approve"))!;
    expect(approve.attributes("disabled")).toBeDefined();
    await wrapper.get('input:not([type])').setValue("operator");
    await wrapper.get('input[type="password"]').setValue("secret");
    await approve.trigger("click");
    await flushPromises();
    expect(api).toHaveBeenCalledWith("/project-toolchain/approvals/dep-1", "POST",
      { approve: true, allow_additional_packages: false },
      { "X-Operator-Id": "operator", "X-Operator-Secret": "secret" });
    expect((wrapper.get('input[type="password"]').element as HTMLInputElement).value).toBe("");
  });
  it("disables approval for viewers", async () => {
    const wrapper = mount(ProjectToolchain, { props: { project: "site", editable: false } });
    await flushPromises();
    await wrapper.get('input:not([type])').setValue("operator");
    await wrapper.get('input[type="password"]').setValue("secret");
    expect(wrapper.findAll("button").filter(b => b.text() !== "Refresh")
      .every(b => b.attributes("disabled") !== undefined)).toBe(true);
  });
});
