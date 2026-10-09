import json
import subprocess
import sys

import pytest

from scripts.requirement_to_output import analyze_requirement

GREENFIELD_REQUIREMENT = (
    "Build a scalable URL shortener service with APIs, persistence, and analytics."
)


@pytest.mark.parametrize(
    ("scenario", "expected_task"),
    [
        ("greenfield", "define-contracts"),
        ("brownfield", "inspect-existing"),
        ("ambiguous", "clarify-requirement"),
    ],
)
def test_shortener_scenarios_generate_ordered_reviewable_outputs(scenario, expected_task):
    result = analyze_requirement(GREENFIELD_REQUIREMENT, scenario)
    outputs = result["engineering_outputs"]
    task_ids = {task["id"] for task in outputs["tasks"]}

    assert result["scenario"] == scenario
    assert result["generation_mode"] == "local-deterministic-rules"
    assert result["human_review_gate"]["implementation_authorized"] is False
    assert outputs["apis"]
    assert outputs["schemas"]
    assert outputs["code_changes"]
    assert outputs["tests"]
    assert outputs["documentation"]
    assert outputs["tasks"][0]["id"] == expected_task
    assert all(set(task["depends_on"]) <= task_ids for task in outputs["tasks"])
    task_positions = {task["id"]: index for index, task in enumerate(outputs["tasks"])}
    assert all(
        task_positions[dependency] < task_positions[task["id"]]
        for task in outputs["tasks"]
        for dependency in task["depends_on"]
    )
    assert all(
        "ai_assistance" in task and "engineer_validation" in task for task in outputs["tasks"]
    )


def test_ambiguous_quality_targets_raise_questions_instead_of_approving_scope():
    result = analyze_requirement("Make URL redirects fast and scalable with analytics.", "auto")

    assert result["scenario"] == "ambiguous"
    assert result["ambiguity"]["is_ambiguous"] is True
    assert any("latency target" in question for question in result["ambiguity"]["items"])
    assert any("request volume" in question for question in result["ambiguity"]["items"])
    assert result["engineering_outputs"]["apis"]
    assert result["engineering_outputs"]["tasks"][0]["id"] == "clarify-requirement"
    assert result["human_review_gate"]["implementation_authorized"] is False


def test_auto_detects_brownfield_language():
    result = analyze_requirement("Improve the existing URL shortener's analytics.")

    assert result["scenario"] == "brownfield"
    assert result["engineering_outputs"]["tasks"][0]["id"] == "inspect-existing"


@pytest.mark.parametrize("requirement", ["", "  \n"])
def test_empty_requirement_is_rejected(requirement):
    with pytest.raises(ValueError, match="must not be empty"):
        analyze_requirement(requirement)


def test_cli_reads_input_file_and_writes_json(tmp_path):
    input_file = tmp_path / "requirement.txt"
    output_file = tmp_path / "proposal.json"
    input_file.write_text(GREENFIELD_REQUIREMENT, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/requirement_to_output.py",
            "--input-file",
            str(input_file),
            "--scenario",
            "greenfield",
            "--output",
            str(output_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["scenario"] == "greenfield"
    assert result["requirement"] == GREENFIELD_REQUIREMENT
