"""Multi-route evidence and backend results cannot be replaced by a model PASS."""

import pytest

from app.tasks.evidence import AcceptanceCriteria, evaluate_acceptance, execution_evidence


@pytest.mark.parametrize(
    "failure", ["missing_route", "http_error", "build_failed", "test_failed", "no_output"]
)
def test_final_pass_cannot_override_incomplete_project_qa(failure: str) -> None:
    previews = [
        {
            "route": route,
            "device": device,
            "dom_loaded": True,
            "render_success": True,
            "http_status": 200,
        }
        for route in ("/", "/work")
        for device in ("desktop", "mobile")
    ]
    operations = {name: {"output": {"passed": True}} for name in ("build", "test")}
    if failure == "missing_route":
        previews.pop()
    elif failure == "http_error":
        previews[-1]["http_status"] = 500
    elif failure == "no_output":
        operations["build"]["output"] = None
    else:
        operations[failure.split("_")[0]]["output"]["passed"] = False
    result = evaluate_acceptance(
        AcceptanceCriteria(required_visual_qa=True, required_project_toolchain=True),
        execution_evidence([]),
        [
            {
                "agent": "reviewer",
                "status": "completed",
                "review": {
                    "verdict": "pass",
                    "summary": "Claimed pass",
                    "findings": [],
                    "acceptance_criteria": [],
                },
            }
        ],
        {
            "screenshots_generated": True,
            "routes": ["/", "/work"],
            "previews": previews,
            "toolchain": operations,
        },
    )
    assert result["status"] == "rejected"
