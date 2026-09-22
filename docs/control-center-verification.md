# Control center stabilization: verification record

This records checks executed against the existing repository during September
2026. Automated tests use mocked model transports; the Ollama and browser checks
below were separate real runs. A successful runtime regression does not mean the
generated UTXO application works.

## 1. Incorrect completion: cause

Worker execution completion previously served as a proxy for task success. A
reviewer could finish normally while reporting defects in prose, and finishing
did not establish that required files had been completely inspected after their
latest writes. Execution status, review judgment, and acceptance now have separate
representations. `result.status` in supervisor results describes execution; use
the top-level task status and `result.acceptance` for acceptance.

## 2. Acceptance model

All selected and explicitly required workers must complete. The deterministic
evaluator checks explicit required files, modifications, ordered readback,
reviewer inspection, tool calls, tests, and review requirements. A missing required
review, failed verdict, critical finding, or failed/unverified required review
criterion rejects the task. A strict PASS requirement also rejects warnings.
Requirements are explicit task options, not automatically inferred from prose.

## 3. Reviewer schema

The reviewer returns `verdict` (`pass`, `pass_with_warnings`, `fail`), `summary`,
`findings`, and `acceptance_criteria`. Findings carry severity, category, message,
affected files, and claimed evidence event IDs. Criteria carry a result and
whether they are required. Finishing without a structured review is invalid.
Model judgments remain claims: citing an event does not prove a defect or its
absence. The UI labels them as model-reviewed.

## 4. Capability-aware delegation

Supervisor prompts include the actual worker tool manifest. Researcher inspection,
coder modification/readback, and reviewer inspection have distinct instructions.
Prior worker summaries and structured claims are passed as untrusted context.
The supervisor still has no tool permissions; broker enforcement remains the
authority. Researcher and reviewer have no write capability.

## 5. Ordered evidence

Only successful broker events establish file evidence. A write invalidates earlier
readback. Complete coverage requires byte ranges from the same actor and file
version; pages from different actors or versions cannot be combined. Reviewer
coverage is recorded separately. Malformed legacy metadata produces visible
evidence warnings instead of being treated as proof.

## 6. Complete filesystem reads

`filesystem.read` accepts byte `offset` and bounded `max_bytes`, returning
`complete`, `next_offset`, returned/total byte counts, and a file version. UTF-8
page boundaries and newline bytes are preserved. Models receive the full bounded
read page, removing the former second 1,500-character observation truncation.
Durable results retain metadata rather than file contents. Workspace confinement
and existing traversal checks remain enforced.

## 7. Reported HTTP 500

The original HTTP 500 could not be reproduced and its cause remains unconfirmed.
No original traceback was available. Existing stored completed, failed, and
interrupted tasks were checked through the current ASGI application; runtime,
task list, task detail, events, and evidence endpoints returned HTTP 200.

Malformed historical tool metadata was identified as a possible projection
failure and hardened, with regression coverage. This is not proof that it caused
the reported incident. Frontend errors now identify the failing HTTP method and
endpoint instead of hiding request failures. SQLite review serialization and
completed/failed/interrupted detail rendering also have regression coverage.

## 8. Real Ollama regression

Repair task: `3e9854e7-7568-4133-835e-99df90c8cceb` in the isolated
`data/stabilization/runtime.db`, using the existing `workspace/utxo-lab` project.
Supervisor, researcher, and reviewer used `qwen3:8b`; coder used
`qwen3-coder:30b`. The fast profile used explicit `reasoning_effort: none` and a
240-second request timeout. Model names are profile configuration, not routing
logic embedded in agents.

The task required complete post-write reads and complete reviewer inspection of
README.md, index.html, styles.css, and app.js, plus PASS with no critical findings.

| Layer | Observed result |
|---|---|
| Researcher execution | COMPLETED, 4 decisions |
| Coder execution | COMPLETED, 8 decisions |
| Reviewer execution | COMPLETED, 4 decisions |
| Reviewer judgment | PASS |
| Task acceptance | FAILED |

The coder only modified and fully read back app.js. The reviewer read the other
three files, but did not inspect app.js. Acceptance correctly rejected missing
post-write evidence for README.md, index.html, and styles.css, and missing
reviewer inspection of app.js. No tests or privileged actions ran in this task.
The model's PASS was therefore insufficient, as intended.

A previous real file task, `229c1232-6d6b-4ffa-9312-f429d4701c10`, completed with
coder write/read and reviewer read of `runtime-check.txt` using the respective
configured local models. This verifies the simpler routing/write/read flow, not
the UTXO repair.

Cancellation task `d068baf3-96ed-4329-829c-f56016b8aa1f` was cancelled during a
researcher request after a supervisor timeout/retry recovery. It ended cancelled,
with zero queued jobs, exactly one cancellation event, no writes, and unchanged
SHA-256 hashes for all four project files. A second slow attempt was also cancelled
before writes. Narrowing structured response schemas by role improved the next
run; these observations do not establish a universal cause for model latency.

## 9. Browser verification

The control center at `http://127.0.0.1:8770/dashboard/` displayed completed
workers, model-reviewed PASS, failed acceptance, and the missing evidence list.
Its browser error log was empty during the check.

The generated project was served at `http://127.0.0.1:8772/`. The page loaded,
but UTXO cards did not render. AUTO SELECT and CLEAR SELECTION did not update the
displayed selection. Amount and fee input edits did not recalculate successfully,
and BUILD TRANSACTION produced no simulation result. The browser recorded:

```text
TypeError: Cannot set properties of null (setting 'textContent')
  at updateTransactionBuilder (app.js:122:58)
  at selectUtxo (app.js:56:3)
  at HTMLDocument.init (app.js:33:3)
```

The UTXO repair is unsuccessful. Absence of external network requests was not
verified with network tracing and is not claimed.

## 10–12. Automated validation

| Command | Result |
|---|---|
| `.venv-control/Scripts/python.exe -m pytest -q` | 192 passed, 1 skipped, 54.09 s |
| `.venv-control/Scripts/ruff.exe check .` | Passed |
| `.venv-control/Scripts/mypy.exe` | Passed, 64 source files |
| `npm.cmd test` in ui | 11 passed |
| `npm.cmd run build` in ui | Passed, Vite 6.4.3 |
| `git diff --check` | Passed |

The single skip was a Windows symlink test where the host lacked the actual
symlink privilege. Existing policy, confinement, prompt injection, broker, and
privileged boundary tests were retained. The working Python environment for
these checks was `.venv-control`; the pre-existing `.venv` referenced an unusable
Store Python installation.

## 13. Remaining limitations and operation

- The UTXO application still has the browser defect above; acceptance correctly
  refuses to certify it.
- The original HTTP 500 cause is unknown without its traceback or reproduction.
- File evidence proves reads/writes at execution time, not semantic correctness.
  Versions use filesystem metadata, not cryptographic content identities, and
  external concurrent writers are not a supported coordination mechanism.
- Review findings and criteria are model judgments. Complete inspection does not
  prove that a PASS judgment is correct.
- Acceptance requirements must be supplied explicitly. Legacy tasks without
  acceptance records display NOT EVALUATED; history is not retroactively certified.
- Model timeouts and output limits remain possible. Retries and terminal errors
  are exposed; tasks do not resume automatically after process restart.
- Background work remains single-process. Startup marks abandoned active tasks
  interrupted, and approval resume remains outside this implementation.

Restart an already-running backend to load Python changes and rebuild the UI as
described in the README. Model profile saves apply while idle without a restart;
active work prevents configuration replacement. Local verification databases and
workspace backups are ignored artifacts, not fixtures required by the test suite.
