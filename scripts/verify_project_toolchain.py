"""Operator-only real Docker/Flask/Vite/Chromium proof; never exposed as an agent tool."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

import httpx

from app.tools.browser import BrowserArgs, BrowserTools
from app.tools.filesystem import Workspace
from app.tools.project import ProjectToolchain
from app.tools.project_manifest import ProjectArgs


async def verify(image: str) -> None:
    root = Path(__file__).resolve().parents[1]  # noqa: ASYNC240 - operator CLI setup
    workspace = Workspace(root / "workspace")
    name = "toolchain-proof-" + uuid.uuid4().hex[:8]
    project = workspace.resolve(name)
    project.mkdir()
    for directory in ("templates", "tests", "static", "assets"):
        (project / directory).mkdir()
    routes = ["/", "/work", "/work/example-project", "/journal", "/studio"]
    # Fixed proof versions, including transitive dependencies explicitly approved below.
    python = {
        "Flask": "3.1.2",
        "Flask-SQLAlchemy": "3.1.1",
        "pytest": "8.4.2",
        "Werkzeug": "3.1.3",
        "Jinja2": "3.1.6",
        "click": "8.2.1",
        "itsdangerous": "2.2.0",
        "blinker": "1.9.0",
        "MarkupSafe": "3.0.2",
        "SQLAlchemy": "2.0.43",
        "greenlet": "3.2.4",
        "typing_extensions": "4.15.0",
        "iniconfig": "2.1.0",
        "packaging": "25.0",
        "pluggy": "1.6.0",
        "Pygments": "2.19.2",
    }
    lock = json.loads((root / "ui/package-lock.json").read_text(encoding="utf-8"))["packages"]
    names = [
        "vite",
        "esbuild",
        "@esbuild/linux-x64",
        "rollup",
        "@rollup/rollup-linux-x64-gnu",
        "@types/estree",
        "postcss",
        "nanoid",
        "picocolors",
        "source-map-js",
        "fdir",
        "picomatch",
        "tinyglobby",
    ]
    npm = {package: lock["node_modules/" + package]["version"] for package in names}
    (project / "project.json").write_text(
        json.dumps(
            {
                "framework": "flask",
                "frontend": "vite",
                "python": python,
                "npm": npm,
                "routes": routes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project / "app.py").write_text(
        """from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///proof.db"
db = SQLAlchemy(app)
class Visit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
with app.app_context():
    db.create_all()
@app.get("/")
@app.get("/work")
@app.get("/work/example-project")
@app.get("/journal")
@app.get("/studio")
def page():
    from flask import request
    return render_template("page.html", title="Toolchain proof " + request.path)
""",
        encoding="utf-8",
    )
    (project / "templates/page.html").write_text(
        """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title>
<style>body{margin:0;background:#102b35;color:#f8eedc;font-family:system-ui}main{padding:6vw;max-width:65rem}h1{font-size:clamp(2rem,6vw,5rem)}a{color:#e8b77e;margin-right:1rem}p{line-height:1.6}</style>
</head><body><main><p>CONTROLLED PROJECT TOOLCHAIN</p><h1>{{ title }}</h1>
<nav><a href="/">Home</a><a href="/work">Work</a>
<a href="/journal">Journal</a><a href="/studio">Studio</a></nav>
<p>Flask + Jinja, SQLite and built Vite assets. Rendered inside an isolated container.</p>
<p id="asset-status">Asset loading</p></main>
<script type="module" src="/static/dist/entry.js"></script></body></html>""",
        encoding="utf-8",
    )
    (project / "assets/entry.js").write_text(
        'document.querySelector("#asset-status").textContent="Vite asset loaded";', encoding="utf-8"
    )
    (project / "vite.config.mjs").write_text(
        """export default {build:{outDir:"static/dist",emptyOutDir:true,
rollupOptions:{input:"assets/entry.js",output:{entryFileNames:"entry.js"}}}};""",
        encoding="utf-8",
    )
    (project / "tests/test_routes.py").write_text(
        """from app import app
import socket
from pathlib import Path
import pytest
def test_routes():
    for path in ["/", "/work", "/work/example-project", "/journal", "/studio"]:
        response = app.test_client().get(path)
        assert response.status_code == 200
        assert b"Toolchain proof" in response.data
def test_network_isolation():
    with pytest.raises(OSError):
        socket.create_connection(("192.168.1.1", 80), timeout=1)
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/runtime").exists()
""",
        encoding="utf-8",
    )
    service = ProjectToolchain(workspace, root / "data/toolchain-proof.db", image)
    args = ProjectArgs(project=name)
    print(json.dumps({"project": str(project), "image": image}), flush=True)
    request = await service.dependencies(args)
    approved = await service.decide(request["request_id"], "operator-cli-proof", True, True)
    if approved["status"] != "executed":
        raise RuntimeError(json.dumps(approved))
    operations = {}
    try:
        for operation in ("build", "test"):
            operations[operation] = await service.operation(operation, args)
            if operations[operation]["status"] != "completed":
                raise RuntimeError(json.dumps(operations[operation]))
            print(operation + ": passed", flush=True)
        serving = await service.operation("serve", args)
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(serving["url"] + "/studio")).status_code == 200
            assert (await client.post(serving["url"] + "/")).status_code == 501
            assert (
                await client.get(serving["url"], headers={"Host": "localhost:1"})
            ).status_code == 403
        await service.stop_task(None)
        async with httpx.AsyncClient(trust_env=False) as client:
            try:
                await client.get(serving["url"], timeout=2)
            except httpx.ConnectError:
                pass
            else:
                raise AssertionError("project.stop left the server listening")
        report = await BrowserTools(workspace, service).capture(BrowserArgs(project=name))
        report.pop("_images", None)
        assert len(report["previews"]) == 10
        assert all(
            p["http_status"] == 200
            and p["render_success"]
            and not p["console_errors"]
            and not p["failed_resources"]
            and not p["horizontal_overflow"]
            for p in report["previews"]
        )
        assert all(
            "Vite asset loaded" in p["document_dimensions"]["visible_text"]
            for p in report["previews"]
        )
        assert not service.executor.servers
        proof = {
            "project": name,
            "image": image,
            "dependency_approval": approved,
            "operations": operations,
            "qa": report,
        }
        (project / "qa/toolchain-proof.json").write_text(
            json.dumps(proof, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "proof": str(project / "qa/toolchain-proof.json"),
                    "routes": 5,
                    "screenshots": 10,
                    "server_stopped": True,
                }
            ),
            flush=True,
        )
    finally:
        await service.stop_task(None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Operator-built immutable Docker image ID")
    asyncio.run(verify(parser.parse_args().image))
