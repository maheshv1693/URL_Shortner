"""Turn a requirement into a reviewable engineering proposal."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCENARIOS = ("greenfield", "brownfield", "ambiguous")
URL_SHORTENER_TERMS = (
    "url shortener",
    "url shortening",
    "url redirect",
    "short url",
    "short link",
)
AMBIGUOUS_TERMS = (
    "fast",
    "faster",
    "scalable",
    "secure",
    "user-friendly",
    "easy",
    "robust",
    "high performance",
)
BROWNFIELD_TERMS = (
    "existing",
    "current",
    "enhance",
    "improve",
    "refactor",
    "bug",
    "regression",
    "update",
)


def _is_url_shortener(requirement: str) -> bool:
    normalized = requirement.lower()
    return any(term in normalized for term in URL_SHORTENER_TERMS)


def _infer_scenario(requirement: str) -> str:
    normalized = requirement.lower()
    if any(term in normalized for term in BROWNFIELD_TERMS):
        return "brownfield"
    if any(term in normalized for term in AMBIGUOUS_TERMS):
        return "ambiguous"
    return "greenfield"


def _questions(requirement: str, is_shortener: bool) -> list[str]:
    normalized = requirement.lower()
    questions: list[str] = []
    if any(term in normalized for term in ("fast", "faster", "low latency", "performance")):
        questions.append("What latency target should be met, and at which percentile and load?")
    if any(term in normalized for term in ("scalable", "scale", "high traffic", "millions")):
        questions.append(
            "What request volume, data size, and growth target should the design support?"
        )
    if any(term in normalized for term in ("secure", "security", "safe")):
        questions.append("Which threat model and security controls are required?")
    if is_shortener and "analytics" in normalized:
        questions.append(
            "Which analytics are required, and what retention and privacy rules apply?"
        )
    if is_shortener and not any(word in normalized for word in ("api", "apis", "endpoint")):
        questions.append("Which API operations and authentication model are expected?")
    if not questions and any(term in normalized for term in AMBIGUOUS_TERMS):
        questions.append("What measurable acceptance criteria define the requested quality?")
    return questions


def _shortener_outputs(scenario: str) -> dict[str, Any]:
    if scenario == "brownfield":
        tasks = [
            {
                "id": "inspect-existing",
                "title": "Inspect the existing URL creation, redirect, and analytics paths",
                "depends_on": [],
                "ai_assistance": (
                    "Trace the current implementation and identify the smallest affected modules."
                ),
                "engineer_validation": (
                    "Confirm the proposed scope against current API behavior and tests."
                ),
            },
            {
                "id": "implement-change",
                "title": "Implement the approved URL-shortener change",
                "depends_on": ["inspect-existing"],
                "ai_assistance": (
                    "Draft a focused patch that follows existing service, schema, and "
                    "error-handling patterns."
                ),
                "engineer_validation": (
                    "Review the diff for compatibility, security, and unintended behavior changes."
                ),
            },
            {
                "id": "add-regression-tests",
                "title": "Add regression and integration tests for the changed behavior",
                "depends_on": ["implement-change"],
                "ai_assistance": (
                    "Suggest boundary cases and tests based on the stated acceptance criteria."
                ),
                "engineer_validation": (
                    "Run the targeted tests and the full suite; verify the regression is "
                    "reproduced before the fix."
                ),
            },
            {
                "id": "update-docs",
                "title": "Update API and setup documentation",
                "depends_on": ["implement-change"],
                "ai_assistance": "Draft documentation changes from the accepted API behavior.",
                "engineer_validation": (
                    "Check examples against the actual OpenAPI schema and implementation."
                ),
            },
        ]
    elif scenario == "ambiguous":
        tasks = [
            {
                "id": "clarify-requirement",
                "title": "Resolve open questions before committing to implementation",
                "depends_on": [],
                "ai_assistance": (
                    "Group unclear performance, scale, API, and analytics terms into concrete "
                    "questions."
                ),
                "engineer_validation": (
                    "Get stakeholder answers and record measurable acceptance criteria."
                ),
            },
            {
                "id": "design-shortener",
                "title": "Propose API, persistence, redirect, and analytics design",
                "depends_on": ["clarify-requirement"],
                "ai_assistance": (
                    "Compare a minimal SQLite prototype with a documented path to distributed "
                    "scaling."
                ),
                "engineer_validation": "Approve the scope and trade-offs before implementation.",
            },
            {
                "id": "implement-shortener",
                "title": "Implement the approved URL-shortener behavior",
                "depends_on": ["design-shortener"],
                "ai_assistance": (
                    "Draft changes in small reviewable units with tests for each component."
                ),
                "engineer_validation": (
                    "Review and run each change; do not accept generated code without verification."
                ),
            },
        ]
    else:
        tasks = [
            {
                "id": "define-contracts",
                "title": "Define URL creation, redirect, management, and analytics contracts",
                "depends_on": [],
                "ai_assistance": (
                    "Draft request/response schemas and HTTP status behavior from the requirement."
                ),
                "engineer_validation": (
                    "Review endpoint scope, authentication, error cases, and API compatibility."
                ),
            },
            {
                "id": "implement-persistence",
                "title": "Implement persistence models and database migration",
                "depends_on": ["define-contracts"],
                "ai_assistance": "Draft SQLAlchemy models, constraints, and migration tests.",
                "engineer_validation": (
                    "Check constraints, ownership boundaries, migration reversibility, and "
                    "data handling."
                ),
            },
            {
                "id": "implement-url-flows",
                "title": "Implement URL creation and redirect flows",
                "depends_on": ["implement-persistence"],
                "ai_assistance": (
                    "Draft routes and service logic for codes, aliases, validation, and redirects."
                ),
                "engineer_validation": (
                    "Review collision behavior, input validation, redirect status, and link safety."
                ),
            },
            {
                "id": "implement-analytics",
                "title": "Implement click analytics and owner-facing reporting",
                "depends_on": ["implement-persistence", "implement-url-flows"],
                "ai_assistance": "Draft ingestion and aggregation logic plus failure-mode tests.",
                "engineer_validation": (
                    "Check privacy, redirect latency, data loss behavior, and concurrency."
                ),
            },
            {
                "id": "verify-and-document",
                "title": "Add tests, validate the service, and document its operation",
                "depends_on": ["implement-url-flows", "implement-analytics"],
                "ai_assistance": (
                    "Suggest unit/integration cases and draft setup/API documentation."
                ),
                "engineer_validation": (
                    "Run lint, migrations, tests, security checks, and a live smoke test."
                ),
            },
        ]

    return {
        "apis": [
            {
                "method": "POST",
                "path": "/api/v1/urls",
                "purpose": (
                    "Create a short URL; authenticate the owner and validate the destination."
                ),
            },
            {
                "method": "GET",
                "path": "/{code}",
                "purpose": (
                    "Redirect to the stored destination and record a click without delaying "
                    "the redirect."
                ),
            },
            {
                "method": "GET",
                "path": "/api/v1/urls/{code}/analytics",
                "purpose": "Return click analytics to the authenticated link owner.",
            },
        ],
        "schemas": [
            {
                "name": "URL",
                "fields": [
                    "id",
                    "code (unique)",
                    "long_url",
                    "owner_id",
                    "created_at",
                    "expires_at",
                    "is_active",
                ],
            },
            {
                "name": "Click",
                "fields": ["id", "url_id", "accessed_at", "ip_hash", "user_agent", "referrer"],
            },
        ],
        "code_changes": [
            {
                "path": "app/models/url.py",
                "purpose": "Persist short codes, destinations, ownership, and expiry state.",
            },
            {
                "path": "app/api/v1/urls.py",
                "purpose": (
                    "Implement authenticated URL creation and owner-facing management/API "
                    "contracts."
                ),
            },
            {
                "path": "app/api/redirect.py",
                "purpose": (
                    "Resolve short codes and issue redirects with defined missing/expired behavior."
                ),
            },
            {
                "path": "app/services/analytics_service.py",
                "purpose": (
                    "Record and aggregate click events while keeping analytics best-effort."
                ),
            },
        ],
        "tests": [
            "URL creation validates input and returns a unique short code.",
            (
                "Redirect returns the expected location and handles unknown, expired, and "
                "disabled codes."
            ),
            "Ownership rules prevent one user from managing another user's links.",
            (
                "Analytics counts and privacy behavior are correct and do not break redirects "
                "on write failure."
            ),
            "Concurrent code creation does not violate uniqueness.",
        ],
        "documentation": [
            "API contract and error/status-code behavior",
            "Local setup, configuration, migration, and test instructions",
            "Scaling limits and the path from SQLite to a production database/cache",
        ],
        "tasks": tasks,
    }


def analyze_requirement(requirement: str, scenario: str = "auto") -> dict[str, Any]:
    """Return a deterministic, human-reviewable engineering proposal."""
    requirement = requirement.strip()
    if not requirement:
        raise ValueError("Requirement text must not be empty.")
    if scenario != "auto" and scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of: {', '.join(SCENARIOS)}")

    is_shortener = _is_url_shortener(requirement)
    resolved_scenario = _infer_scenario(requirement) if scenario == "auto" else scenario
    questions = _questions(requirement, is_shortener)
    is_ambiguous = resolved_scenario == "ambiguous" or bool(questions)

    if is_shortener:
        engineering = _shortener_outputs(resolved_scenario)
        intent = (
            "Build or change a URL-shortening service that creates unique short links, redirects "
            "visitors, persists link data, and reports click analytics."
        )
    else:
        engineering = {
            "apis": [],
            "schemas": [],
            "code_changes": [],
            "tests": [],
            "documentation": [],
            "tasks": [
                {
                    "id": "clarify-and-design",
                    "title": "Clarify the requirement and identify affected components",
                    "depends_on": [],
                    "ai_assistance": (
                        "Suggest a decomposition after the requirement is made specific."
                    ),
                    "engineer_validation": (
                        "Confirm scope, acceptance criteria, and affected code with stakeholders."
                    ),
                }
            ],
        }
        intent = "Convert the supplied requirement into a scoped engineering change."

    return {
        "schema_version": "1.0",
        "generation_mode": "local-deterministic-rules",
        "scenario": resolved_scenario,
        "requirement": requirement,
        "clarified_intent": intent,
        "ambiguity": {
            "is_ambiguous": is_ambiguous,
            "items": questions,
            "questions_for_stakeholder": questions,
        },
        "assumptions": [
            "The output is a proposal, not an approved specification or an executed code change.",
            (
                "An engineer reviews every assumption, task, API, schema, and validation result "
                "before implementation."
            ),
            (
                "The initial URL-shortener example targets a single-service prototype; "
                "production scale targets must be confirmed."
            ),
        ],
        "engineering_outputs": engineering,
        "validation_plan": [
            "Review open questions and acceptance criteria with the requirement owner.",
            ("Review generated design and code-change proposals against existing architecture."),
            (
                "Add unit and integration tests for success, boundary, failure, and "
                "authorization behavior."
            ),
            "Run targeted tests, the full pytest suite, and ruff check .; inspect the final diff.",
            (
                "For URL-shortener changes, run API smoke tests and verify redirect and "
                "analytics behavior."
            ),
        ],
        "risks_and_tradeoffs": [
            {
                "risk": (
                    "Keyword-based analysis can miss context or interpret domain terms incorrectly."
                ),
                "mitigation": "Treat results as drafts and require engineer/stakeholder review.",
            },
            {
                "risk": (
                    "A small prototype's SQLite and in-process components do not provide "
                    "distributed scale."
                ),
                "mitigation": (
                    "Measure the agreed targets and document database, cache, and deployment "
                    "changes before production use."
                ),
            },
        ],
        "human_review_gate": {
            "implementation_authorized": False,
            "next_action": "Resolve questions and approve the proposal before making code changes.",
        },
    }


def _read_requirement(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if args.requirement and args.input_file:
        parser.error("provide requirement text or --input-file, not both")
    if args.input_file:
        try:
            return args.input_file.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read {args.input_file}: {exc}")
    if args.requirement:
        return args.requirement
    parser.error("provide requirement text or --input-file")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a local, deterministic, human-reviewable engineering proposal."
    )
    parser.add_argument("requirement", nargs="?", help="Requirement text to analyze.")
    parser.add_argument("--input-file", type=Path, help="Read requirement text from a UTF-8 file.")
    parser.add_argument(
        "--scenario",
        choices=("auto", *SCENARIOS),
        default="auto",
        help="Scenario type; defaults to keyword-based inference.",
    )
    parser.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout.")
    args = parser.parse_args(argv)
    requirement = _read_requirement(args, parser)

    try:
        result = analyze_requirement(requirement, args.scenario)
    except ValueError as exc:
        parser.error(str(exc))

    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
