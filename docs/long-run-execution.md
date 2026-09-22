# Optional long-run execution verification

Implemented on the existing worker/broker/task-store workflow. No alternate model
executor, arbitrary browser API or new privilege path is introduced.

## Execution and acceptance

- Defaults: profile worker steps and two real visual repairs.
- Explicit custom worker steps have no artificial 50-step validation ceiling.
- Unlimited worker steps use `None` plus `worker_steps_mode=unlimited`; unlimited
  repair uses `None`. The quality checkbox alone changes neither limit.
- Each cycle copies the configured workers and starts new `run` invocations.
  Continuation includes only recent evidence, latest findings/QA and a short summary.
  Unlimited workers keep at most thirteen messages, including the original goal.
- A changed source write and rendered QA are required to count a real repair.
  No-op writes, unrelated project writes and generated QA artifacts do not count.
- Three consecutive no-repair attempts stall. Three repeated critical/warning
  finding comparisons without modifications to affected files also stall. Warning
  and info findings map to the existing major/minor cycle display fields.
- Unlimited worker loops also stall on repeated invalid decisions, missing tools or
  identical tool results. Cancellation remains cooperative at each decision and
  interrupts outstanding model requests; browser cancellation drains cleanup.
- There is no whole-task clock deadline. Individual bounded browser/provider calls,
  errors, approvals and policy restrictions retain their existing behavior.
- Long-run and unlimited visual repair enforce final PASS, clean desktop/mobile QA,
  index/report evidence and complete readback. Existing explicit evidence checks
  remain additive. A generated screenshot is not proof of model pixel inspection.
- The first verified PASS in quality mode triggers one polish invocation, even at
  the repair budget boundary. Polish is separate from counted repairs. If it
  regresses, normal repairs resume within the selected remaining budget.

## Durable recovery

Each stage checkpoints cycle, mode, latest review/QA, acceptance, progress and
polish status. Compact cycle records are appended to the existing audit stream;
the task result keeps the latest twenty. SQLite recovery marks in-flight unlimited
or long-run tasks PAUSED, preserving their checkpoint and options.

Resume is operator-only, rejects unresolved approvals and starts fresh invocations.
Visual resume first restricts Coder to read-only inspection, captures new QA and
reviews current files before any repair. Other resumed workers receive compact
file evidence and the previous checkpoint, with instructions to inspect current
state and avoid replay. Completed/cancelled tasks cannot be resumed.

## Verification

Backend regressions cover explicit limits, 61 worker decisions, bounded context,
fresh cycles beyond two repairs, no-op/unchanged finding stalls, cancellation,
polish/no-change polish/regression, final verified acceptance, SQLite recovery and
operator-only visual/nonvisual resume. Existing real Chromium tests serve and
capture temporary localhost pages, including FastAPI on Windows Selector loops.
Studio-26 is not modified by this change or these tests.

UI tests cover default/custom/unlimited submission, Cycle 7 / infinity, progress
and Cancel/Resume. Required checks: `pytest -q`, `ruff check .`, `mypy`, `npm test`
and `npm run build` (the npm commands run inside `ui`).

## Limits

Progress uses observed changed bytes and affected file paths, not a semantic proof
that edits improve quality. External edits are revalidated on resume; the runtime
does not restore an in-flight model request or automatically replay actions.
Operator resume resets stagnation detection to permit work after intervention.
Audit/database history still grows with real execution; only model context and
the result's cycle window are bounded. Screenshots remain fixed initial viewports,
not interaction tests or full-page coverage. Pixel inputs require a vision-enabled
model profile. Infrastructure failure stops with its actual diagnostic.
