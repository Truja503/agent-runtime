# studio-26 workflow stabilization

## Root causes

The runtime Python (`.venv-control/Scripts/python.exe`, Playwright 1.63.0)
successfully launched Chromium with a Windows Proactor loop. The same capture
under `_WindowsSelectorEventLoop` reproduced `NotImplementedError` in
`asyncio.base_events._make_subprocess_transport`, called by Playwright's driver
transport. Installed Uvicorn selects a Selector loop for its Windows subprocess
mode (including reload). The old generic installation message obscured this cause.

A missing matching Chromium executable is a separate failure. Python package
presence does not establish executable availability or launchability. Readiness
now checks all three and retains exception type/message. The runtime explicitly
uses Playwright's Chromium channel, matching the executable path it reports.

Approval requests rejected by the privileged parser were previously returned as
`approval_required`. The agent accumulated these IDs as pending even though the
operator's pending list correctly contained no live request. Waiting tasks also
lacked a result-delivery continuation and terminal-state reconciliation.

## Changes and boundaries

- Browser capture runs on its own thread/event loop (Proactor on Windows).
  Cancellation waits for cleanup. The global ASGI loop policy is unchanged.
- Readiness is based on a real launch, exposed in the operator UI and through
  authenticated `POST /browser/readiness`. Missing dependencies return a failed
  `browser_unavailable` result. Setup is operator-only:
  `.venv-control/Scripts/python.exe scripts/setup_browser.py`.
- Exact-origin GET/HEAD, confined files, CSP, network/worker/WebSocket/WebRTC
  blocking, and fixed screenshots remain enforced. Models get no Playwright APIs.
- AUTO includes Supervisor planning and required Researcher/Web execution before
  Coder/QA/Reviewer. Failed required research blocks work unless the operator
  explicitly permits degraded research. Research-only tasks use the same gate.
- Repairs receive the previous verdict, complete browser failure, and missing
  acceptance evidence. Changed source bytes inside the selected project are
  required. Identical writes, unrelated writes, QA-only writes, no-op finishes,
  and failed browser startup do not consume a real repair cycle. No-op stops with
  `NO_REPAIR_PERFORMED` / `REPAIR_NOT_PERFORMED`. The bound remains two repairs.
- Active approvals suspend and resume the same agent continuation with the
  operator's result, or a rejected/expired/missing failure. No automatic action
  resubmission or self-approval was added. Request IDs/statuses are shown in UI.
- Startup, periodic reconciliation and console reads clear orphaned terminal
  waits. Existing acceptance, readback, reviewer inspection, and final-verdict
  requirements remain in force.
- Task lists/headings use short titles. Full instructions and verbose summaries
  are available in collapsed details rather than dominating the page.

## Real project proof

`studio-26-qa-proof.json` records the 2026-09-21 capture, readiness, source hashes
before/after capture, PNG hashes/dimensions, and measured outcomes. The existing
website was rendered without source edits during the diagnostic:

| Artifact | Verified result |
| --- | --- |
| `workspace/studio-26/qa/desktop.png` | Actual Chromium PNG, 1440 x 1000 |
| `workspace/studio-26/qa/mobile.png` | Actual Chromium PNG, 390 x 844 |
| `workspace/studio-26/qa/report.json` | Actual DOM, console, resource, viewport and scroll metrics |

Both screenshots were inspected. Each viewport recorded seven console errors
and six failed resources: external font/GSAP/Lenis resources are blocked by the
existing local-only security policy, and `Lenis is not defined`. Neither viewport
reported document horizontal overflow. Scroll heights were 8487 and 9575.
These are successful diagnostic captures, **not a passing website verdict**.

The Windows regression test also runs a real FastAPI/Uvicorn HTTP server on a
Selector loop and proves localhost serving, DOM loading, screenshots, and console
inspection. Other tests exercise dependency absence, launch failures, cancellation
cleanup, repair bounds, required research, and approval terminal outcomes.

## Known limitations

Browser QA serves static `index.html` projects and captures the initial viewport;
it does not execute arbitrary development commands or test interactions. External
CDN dependencies stay blocked. Screenshots require a vision-capable configured
model for pixel inspection; text-only reviewers receive rendered measurements.

Approval continuations survive operator decisions in the running process, not a
process restart. On restart, previously active work is interrupted; orphaned
approval completion is recorded explicitly without replaying code or privileged
actions. Required Web search also needs an operator-configured search provider;
unavailability is reported instead of silently claiming research.

## Final verification

- `pytest -q`: 298 passed, 1 skipped (Windows symlink privilege), 82.73 seconds.
- `ruff check .`: passed.
- `mypy`: passed, 68 source files.
- `npm test`: 17 passed.
- `npm run build`: passed (Vue type check and Vite production build).
- `git diff --check`: passed.
- Live dashboard at port 8770: real browser READY, short task titles, collapsed
  task instructions and agent summaries verified. Existing rejected acceptance
  remains visibly rejected even when the historical reviewer claimed PASS.
