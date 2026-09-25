# Controlled Flask project toolchain

Generated Flask/Jinja projects can request dependencies, build optional Vite assets,
run pytest and render routes without a shell tool. The existing Supervisor/research,
Coder, Reviewer and acceptance flow remains in place.

## Operator setup

Use Docker Desktop with Linux containers (or a Linux Docker engine). Build the trusted
adapter image from this repository, then configure its immutable local image ID:

```powershell
docker build --platform linux/amd64 -t agent-runtime-toolchain:local toolchain
docker image inspect --format '{{.Id}}' agent-runtime-toolchain:local
# Put the returned sha256:... value in .env:
# PROJECT_TOOLCHAIN_IMAGE=sha256:...
```

Restart the runtime after changing this setting. Runtime operations use `--pull=never`;
agents cannot select images or executables. Docker is discovered in PATH and the standard
Windows machine-wide and per-user Docker Desktop installation locations. No host-code
fallback is provided when Docker or the configured image is unavailable. Existing Chromium
installation in the runtime Python environment is still required for browser QA.

## Project contract

Select **Flask + Jinja** and a relative project directory in the task form. API tasks set
`project_framework: "flask"` and `visual_project: "my-app"`. Export the Flask application
as `app` in `app.py`, place templates in `templates/` and backend tests in `tests/`.

`project.json` is data only. For example, this is the shape of a declaration, not a
complete installable dependency closure:

```json
{
  "schema_version": 1,
  "framework": "flask",
  "frontend": "none",
  "python": {"Flask": "3.1.2", "pytest": "8.4.2"},
  "npm": {},
  "routes": ["/", "/work", "/work/example-project", "/journal", "/studio"]
}
```

**Declare every transitive dependency with an exact version as well.** The runtime does
not silently resolve/install undeclared packages. Missing Python dependencies fail `pip
check`; missing required npm dependencies fail the offline dependency metadata check. Python
versions support one through four numeric components; npm versions require three. URLs,
paths, git specifications, version ranges, install flags, command fields and duplicate
normalized package names are rejected. Routes are plain absolute paths within the origin,
without query strings, encoding or traversal, limited to 20.

Use `frontend: "vite"` to build assets with the known Vite binary. A Vite config can set
`static/dist` as the output; Flask templates must reference the generated files. There
are no arbitrary `package.json` script entry points. `.venv/` and `node_modules/` belong
only to the selected project and are Linux environments, not Windows host executables.

## Tools and approval

All six tools accept only `{"project": "my-app"}`:

| Tool | Runtime-owned operation |
|---|---|
| `project.inspect` | Framework, dependencies, environment, build/test/server and Docker status |
| `project.dependencies` | Create an approval request bound to the exact manifest and image |
| `project.build` | Fixed Vite binary, or successful no-build result for plain Jinja |
| `project.test` | Project-local pytest through the fixed test adapter |
| `project.serve` | Fixed Flask adapter and temporary loopback GET/HEAD relay |
| `project.stop` | Close relay and remove its container, even if the manifest was edited |

The Project Toolchain panel displays pending declarations and installation failures.
Approval needs the existing separate Operator ID/secret as well as API authentication.
An API bearer token or model decision cannot approve an installation. Flask,
Flask-SQLAlchemy, pytest, gsap, lenis, three and vite are recognized packages; additional
packages, including transitive packages, require explicit additional-package approval.
All new manifests require approval, including changes to exact versions. Reusing the same
installed manifest/image does not reinstall. Editing the manifest invalidates approval.

Only approved artifacts are downloaded by the runtime, over public-IP-pinned HTTPS from
PyPI/Python-hosted files or the npm registry. Redirects and private DNS answers are denied.
Hashes are checked. Python installation is binary-wheel-only, offline and without dependency
resolution; npm packages are safely extracted and checked without lifecycle scripts.
The operator approval database and temporary download directory are outside the model workspace.

## Execution and network boundary

Generated Python, pytest, Vite configuration and JavaScript run in Linux containers with
no network, no Docker socket, no host runtime mount, a read-only root filesystem, a non-root
UID, no Linux capabilities and `no-new-privileges`. Only the selected project is writable;
installation additionally receives a read-only approved-artifact mount. Containers have
CPU, memory, PID, output and wall-clock limits. Installation permits 600 seconds to account
for Windows bind-mount performance; builds/tests permit 120 seconds. Servers expire after
180 seconds and are also stopped on QA completion, cancellation and runtime shutdown.
Startup recovery removes containers labelled for this runtime workspace.

Flask binds to loopback **inside** its network-disabled container. No container port is
published. A trusted fixed `docker exec` adapter relays bounded HTTP response data to an
ephemeral host `127.0.0.1` port. The relay permits GET/HEAD only, checks the Host header,
and does not forward arbitrary headers or external redirects. Chromium permits only that
exact origin, GET/HEAD, and blocks external requests, sockets, service workers and downloads.
Agents never receive arbitrary Playwright methods or a selectable browser URL.

The Tool Broker confines all filesystem/project/browser operations for Flask tasks to
the selected project; environment writes and the legacy host `tests.run` tool are denied.
Policy Engine and existing privileged-approval rules still apply. Containers isolate
generated code; they do not protect against a compromised Docker daemon or kernel.

## QA, repair and evidence

The flow is Coder → approved dependencies → build → test → temporary serve → browser QA
→ Reviewer. Required research still precedes Coder through the existing AUTO routing.
Build/test/startup diagnostics become repair context. A repair with no actual source change
is recorded as `REPAIR_NOT_PERFORMED` and consumes no real repair cycle. A source-changing
repair consumes a cycle even when its build/test still fails, preventing unbounded failed
build retries in bounded mode. Existing explicit long-run/unlimited controls remain intact.

`browser.preview`, `browser.screenshot` and `browser.console_errors` accept optional `routes`;
otherwise Flask uses manifest routes. Every route gets desktop 1440×1000 and mobile 390×844
captures by default. `qa/report.json` contains per-route HTTP status, title, console errors,
failed resources, overflow, document dimensions, render success and screenshot hashes/paths.
The `/` images are `qa/desktop.png` and `qa/mobile.png`; other paths use stable route-index
filenames. Runtime checks require every declared route at both viewports. Reviewer receives
the actual images and distinguishes source inspection, visual inspection and runtime errors.
Final acceptance requires passing build/test, screenshots and the final Reviewer verdict;
existing readback and inspection checks are preserved.

## Reproducible real proof and limitations

```powershell
.venv-control\Scripts\python.exe scripts/verify_project_toolchain.py --image sha256:YOUR_IMAGE_ID
```

This operator-only diagnostic creates a fresh ignored workspace fixture, explicitly approves
its complete dependency manifest, installs Flask/SQLAlchemy/pytest/Vite, builds assets, tests
five routes and network isolation, checks relay restrictions/cleanup, and captures ten images.
It saves `qa/report.json` and `qa/toolchain-proof.json`. It never rebuilds existing user projects.
Unit tests run without Docker; the diagnostic deliberately requires Docker, registry access
and installed Chromium, so it is not silently skipped as part of ordinary pytest runs.

Initial support is CPython 3.12/Linux amd64 and compatible binary wheels. No source builds,
arbitrary commands, package hooks, external application services, browser form submissions,
authenticated sessions or interactive QA are supported. Approving additional packages is an
operator trust decision. Browser QA checks initial rendered routes, not full application
correctness. Windows bind mounts can make dependency installation slower than native Linux.
