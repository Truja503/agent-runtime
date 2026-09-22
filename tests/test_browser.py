"""Local Chromium plus mocked repair orchestration. No external internet required."""

from __future__ import annotations

import json
import struct
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.config import ProviderKind
from app.container import Runtime
from app.errors import ToolExecutionError
from app.models.base import Message, ModelRequest, Role
from app.models.scripted import ScriptedModelProvider
from app.observability.events import EventType
from app.policy.permissions import AgentRole, Principal
from app.tasks.state import TaskOptions
from app.tools.broker import ToolInvocation
from app.tools.browser import BrowserArgs, BrowserTools, allowed_request, local_server
from app.tools.filesystem import Workspace


@pytest.mark.parametrize(
    "url,method,allowed",
    [
        ("http://127.0.0.1:1234/index.html", "GET", True),
        ("http://127.0.0.1:1234/style.css", "HEAD", True),
        ("http://127.0.0.1:1234/upload", "POST", False),
        ("http://127.0.0.1:1234/upload", "PUT", False),
        ("http://127.0.0.1:9000/", "GET", False),
        ("http://192.168.1.1/", "GET", False),
        ("http://10.0.0.1/", "GET", False),
        ("https://example.com/", "GET", False),
        ("file:///etc/passwd", "GET", False),
        ("http://user:pass@127.0.0.1:1234/", "GET", False),
    ],
)
def test_browser_origin_and_method_boundary(url: str, method: str, allowed: bool) -> None:
    assert allowed_request(url, method, "http://127.0.0.1:1234/") is allowed


async def test_server_confines_files_and_stops(workspace: Workspace) -> None:
    project = workspace.root / "site"
    project.mkdir()
    (project / "index.html").write_text("<h1>Local</h1>")
    (project / ".env").write_text("PRIVATE")
    async with local_server(project) as url, httpx.AsyncClient() as client:
        assert (await client.get(url)).status_code == 200
        for path in [".env", "%2e%2e/README.md", "qa/report.json", "missing.py"]:
            assert (await client.get(url + path)).status_code == 404
        assert (await client.post(url, content="upload")).status_code == 405
        assert (await client.put(url, content="upload")).status_code == 405
        assert (await client.get(url, headers={"Host": "attacker.local"})).status_code == 403
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(url)


async def test_real_render_captures_both_viewports_and_runtime_errors(workspace: Workspace) -> None:
    project = workspace.root / "site"
    project.mkdir()
    (project / "index.html").write_text(
        """<!doctype html><title>Rendered QA</title>
    <style>body {margin:0} .wide {width:1800px}</style><h1>Rendered</h1>
    <div class="wide">Overflow</div><img src="missing.png">
    <script>document.body.dataset.ready='yes'; console.error('fixture-console');
    fetch('http://127.0.0.1:9/private').catch(()=>{});
    fetch('/upload',{method:'POST',body:'blocked'}).catch(()=>{});
    throw new Error('fixture-runtime');</script>""",
        encoding="utf-8",
    )
    result = await BrowserTools(workspace).capture(BrowserArgs(project="site"))
    assert result["screenshots_generated"] and result["server_stopped"]
    assert len(result["_images"]) == 2
    for preview in result["previews"]:
        assert preview["title"] == "Rendered QA" and preview["dom_loaded"]
        assert preview["horizontal_overflow"]
        assert "Rendered" in preview["document_dimensions"]["visible_text"]
        assert any("fixture-runtime" in e for e in preview["console_errors"])
        assert any("fixture-console" in e for e in preview["console_errors"])
        assert any("missing.png" in r["url"] for r in preview["failed_resources"])
        assert any("upload" in r["url"] for r in preview["failed_resources"])
        png = workspace.resolve(preview["screenshot"]).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", png[16:24]) == (
            preview["viewport"]["width"],
            preview["viewport"]["height"],
        )
    report = json.loads((project / "qa" / "report.json").read_text())
    assert report["screenshots"] == ["site/qa/desktop.png", "site/qa/mobile.png"]
    assert "_images" not in report
    (project / "index.html").write_text("<title>Repaired</title><h1>Ready</h1>")
    fresh = await BrowserTools(workspace).capture(BrowserArgs(project="site", viewport="desktop"))
    assert fresh["previews"][0]["console_errors"] == []
    assert fresh["previews"][0]["failed_resources"] == []
    assert not fresh["previews"][0]["horizontal_overflow"]
    assert fresh["screenshots"] == ["site/qa/desktop.png"]


@pytest.mark.parametrize("project", ["../outside", "/etc", "C:\\Windows", "https://example.com"])
async def test_browser_cannot_open_arbitrary_locations(workspace: Workspace, project: str) -> None:
    with pytest.raises(ToolExecutionError):
        await BrowserTools(workspace).capture(BrowserArgs(project=project))


def test_no_arbitrary_browser_controls() -> None:
    for field in ["url", "script", "evaluate", "headers", "upload", "executable_path"]:
        with pytest.raises(ValidationError):
            BrowserArgs.model_validate({"project": "site", field: "arbitrary"})
    with pytest.raises(ValidationError):
        BrowserArgs(project="site", viewport="arbitrary")  # type: ignore[arg-type]
    assert TaskOptions(visual_project="site", max_repair_cycles=3).max_repair_cycles == 3


async def test_browser_roles_preserve_permissions(runtime: Runtime) -> None:
    for role in (AgentRole.WEB, AgentRole.RESEARCHER, AgentRole.SUPERVISOR):
        result = await runtime.broker.invoke(
            Principal(
                name=role.value,
                role=role,
                model_kind=ProviderKind.LOCAL,
                allowed_tools=frozenset({"browser.preview"}),
            ),
            ToolInvocation(tool="browser.preview", arguments={"project": "site"}),
        )
        assert result.status == "denied"
    for role in ("coder", "reviewer"):
        assert "browser.screenshot" in runtime.workers[role].allowed_tools


def mock_qa(runtime: Runtime, *, fail: bool = False) -> list[str]:
    runtime.supervisor.model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps({"workers": ["coder", "reviewer"], "plan": "Build and review"})
            ]
        }
    )
    calls: list[str] = []

    async def capture(args: BaseModel) -> dict[str, Any]:
        calls.append("qa")
        if fail:
            raise ToolExecutionError("fixture browser unavailable")
        return {
            "project": "site",
            "screenshots_generated": True,
            "previews": [
                {
                    "device": device,
                    "dom_loaded": True,
                    "console_errors": [],
                    "failed_resources": [],
                    "horizontal_overflow": False,
                }
                for device in ("desktop", "mobile")
            ],
            "screenshots": ["site/qa/desktop.png", "site/qa/mobile.png"],
            "_images": ["test-png-base64"],
        }

    spec = runtime.registry.get("browser.screenshot")
    runtime.registry._tools[spec.name] = spec.model_copy(update={"handler": capture})
    return calls


def review(verdict: str, *, actionable: bool = True) -> str:
    return json.dumps(
        {
            "action": "finish",
            "summary": verdict,
            "review": {
                "verdict": verdict,
                "summary": verdict,
                "visual_inspection": "screenshots_provided",  # Untrusted claim must be corrected.
                "findings": [
                    {
                        "severity": "critical",
                        "category": "VISUAL INSPECTION",
                        "message": "Fix overflow",
                        "affected_files": ["site/index.html"],
                    }
                ]
                if verdict == "fail" and actionable
                else [],
                "acceptance_criteria": [],
            },
        }
    )


@pytest.mark.parametrize(
    "verdicts,limit,expected,passes",
    [
        (["fail", "pass"], 1, "completed", 2),
        (["fail", "fail", "pass"], 2, "completed", 3),
        (["fail", "fail", "fail", "pass"], 2, "failed", 3),
        (["fail", "pass"], 0, "failed", 1),
    ],
)
async def test_bounded_repairs_use_final_review(
    runtime: Runtime, verdicts: list[str], limit: int, expected: str, passes: int
) -> None:
    qa_calls = mock_qa(runtime)
    model = ScriptedModelProvider(
        script={
            "coder": [
                json.dumps({"action": "finish", "summary": "coded"}),
                *[
                    item
                    for i in range(2)
                    for item in (
                        json.dumps(
                            {
                                "action": "use_tool",
                                "tool": "filesystem.write",
                                "arguments": {"path": "site/index.html", "content": f"repair {i}"},
                            }
                        ),
                        json.dumps({"action": "finish", "summary": "repaired"}),
                    )
                ],
            ],
            "reviewer": [review(v) for v in verdicts],
        }
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Fix site",
        created_by="test",
        options=TaskOptions(visual_project="site", max_repair_cycles=limit),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status.value == expected
    assert len(qa_calls) == passes
    assert len(result.result["repair_attempts"]) == passes
    assert result.result["review"]["verdict"] == verdicts[passes - 1]
    assert result.result["review"]["visual_inspection"] == "rendered_dom_only"
    events = await runtime.tasks.events_for(task.id)
    ordered = [
        (e.actor, e.type)
        for e in events
        if e.type == EventType.AGENT_STARTED
        or (e.type == EventType.TOOL_COMPLETED and e.payload.get("tool") == "browser.screenshot")
    ]
    assert ordered[:4] == [
        ("supervisor", EventType.AGENT_STARTED),
        ("coder", EventType.AGENT_STARTED),
        ("reviewer", EventType.TOOL_COMPLETED),
        ("reviewer", EventType.AGENT_STARTED),
    ]


async def test_failed_capture_cannot_become_visual_acceptance(runtime: Runtime) -> None:
    mock_qa(runtime, fail=True)
    model = ScriptedModelProvider(
        script={"coder": [json.dumps({"action": "finish"})], "reviewer": [review("pass")]}
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Fix site", created_by="test", options=TaskOptions(visual_project="site")
    )
    await runtime.run_task(task.id)
    task = await runtime.tasks.get(task.id)
    assert task.status.value == "failed"
    assert task.result["review"]["visual_inspection"] == "not_performed"
    assert "missing final visual QA screenshots" in task.result["acceptance_failures"]


async def test_no_actionable_findings_does_not_retry(runtime: Runtime) -> None:
    calls = mock_qa(runtime)
    model = ScriptedModelProvider(
        script={
            "coder": [json.dumps({"action": "finish"})],
            "reviewer": [review("fail", actionable=False)],
        }
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Fix site",
        created_by="test",
        options=TaskOptions(visual_project="site", max_repair_cycles=2),
    )
    await runtime.run_task(task.id)
    assert len(calls) == 1


async def test_images_are_ephemeral_and_only_sent_to_opted_in_models(runtime: Runtime) -> None:
    mock_qa(runtime)
    requests: list[ModelRequest] = []

    class Recording(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> Any:
            requests.append(request)
            return await super().generate(request)

    model = Recording(
        script={"coder": [json.dumps({"action": "finish"})], "reviewer": [review("pass")]}
    )
    for worker in runtime.workers.values():
        worker.model = model
    runtime.workers["reviewer"].profile = runtime.workers["reviewer"].profile.model_copy(
        update={"supports_images": True}
    )
    task = await runtime.tasks.create(
        "Fix site", created_by="test", options=TaskOptions(visual_project="site")
    )
    await runtime.run_task(task.id)
    assert any(m.images for r in requests for m in r.messages)
    assert "test-png-base64" not in json.dumps([r.model_dump() for r in requests])
    assert "test-png-base64" not in (await runtime.tasks.get(task.id)).model_dump_json()
    assert "test-png-base64" not in json.dumps(
        [e.model_dump(mode="json") for e in await runtime.tasks.events_for(task.id)]
    )
    assert Message(role=Role.USER, content="hi", images=["png"]).model_dump() == {
        "role": Role.USER,
        "content": "hi",
    }


async def test_image_transport_shaping_without_live_providers() -> None:
    from pydantic import SecretStr

    from app.models.anthropic import AnthropicProvider
    from app.models.local import LocalModelProvider
    from tests.test_providers import FakeAnthropicClient, FakeOpenAIClient

    request = ModelRequest(messages=[Message(role=Role.USER, content="Inspect", images=["png"])])
    openai = FakeOpenAIClient()
    local = LocalModelProvider(
        base_url="http://localhost/v1", api_key=SecretStr("unused"), model="vision", client=openai
    )
    await local.generate(request)
    assert (
        openai.captured["messages"][0]["content"][1]["image_url"]["url"]
        == "data:image/png;base64,png"
    )
    anthropic = FakeAnthropicClient()
    cloud = AnthropicProvider(api_key=SecretStr("unused"), model="vision", client=anthropic)
    await cloud.generate(request)
    assert anthropic.captured["messages"][0]["content"][1]["source"]["data"] == "png"
