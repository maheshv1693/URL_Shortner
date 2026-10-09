# Requirement-to-Engineering Proposal Prototype

## Purpose and architecture

`scripts/requirement_to_output.py` is a runnable, local command-line prototype. It accepts a requirement
as an argument or UTF-8 text file and emits a JSON proposal containing:

- interpreted intent, inferred scenario, ambiguities, and stakeholder questions
- assumptions, ordered tasks with dependencies, and per-task AI assistance and engineer review
- proposed URL-shortener APIs, schemas, code-change locations, tests, and documentation
- validation steps, risks, trade-offs, and an explicit human approval gate

The prototype uses deterministic keyword rules and does not call an AI model, access the network, edit
source files, or run generated changes. Its proposals are illustrative drafts, not generated or approved
implementation. The existing URL shortener and the AI-assisted development evidence are described in
the [project interaction log](./AI_interaction_log.MD). AI runtime integration is outside this local
prototype's current scope.

## Run it

From the repository root:

```bash
python scripts/requirement_to_output.py \
  "Build a scalable URL shortener service with APIs, persistence, and analytics." \
  --scenario greenfield
```

Save the JSON proposal to a file:

```bash
python scripts/requirement_to_output.py \
  --input-file examples/requirements/greenfield_url_shortener.txt \
  --scenario greenfield \
  --output proposal.json
```

Select `--scenario brownfield` or `--scenario ambiguous` to demonstrate those review paths. With no
scenario argument, the tool infers one from a small set of keywords. `--help` lists all options.

## Example scenarios

### Greenfield — mandatory URL-shortener use case

**Input**

> Build a scalable URL shortener service with APIs, persistence, and analytics.

**Command**

```bash
python scripts/requirement_to_output.py \
  "Build a scalable URL shortener service with APIs, persistence, and analytics." \
  --scenario greenfield
```

**Representative output**

```json
{
  "scenario": "greenfield",
  "ambiguity": {
    "is_ambiguous": true,
    "questions_for_stakeholder": [
      "What request volume, data size, and growth target should the design support?",
      "Which analytics are required, and what retention and privacy rules apply?"
    ]
  },
  "engineering_outputs": {
    "apis": [
      {
        "method": "POST",
        "path": "/api/v1/urls",
        "purpose": "Create a short URL; authenticate the owner and validate the destination."
      }
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
          "is_active"
        ]
      }
    ],
    "code_changes": [
      {
        "path": "app/models/url.py",
        "purpose": "Persist short codes, destinations, ownership, and expiry state."
      }
    ],
    "tasks": [
      {
        "id": "define-contracts",
        "title": "Define URL creation, redirect, management, and analytics contracts",
        "depends_on": [],
        "ai_assistance": "Draft request/response schemas and HTTP status behavior from the requirement.",
        "engineer_validation": "Review endpoint scope, authentication, error cases, and API compatibility."
      }
    ]
  },
  "human_review_gate": {
    "implementation_authorized": false,
    "next_action": "Resolve questions and approve the proposal before making code changes."
  }
}
```

This is an abbreviated excerpt of the CLI JSON; the command emits all three API proposals, every
ordered task, validation plan, assumptions, risks, and review gate. “Scalable” triggers clarification
instead of being treated as a measurable production target.

### Brownfield — enhancement to the existing service

**Input**

> Enhance the existing URL shortener to improve analytics reporting.

**Command**

```bash
python scripts/requirement_to_output.py \
  "Enhance the existing URL shortener to improve analytics reporting." \
  --scenario brownfield
```

The same input is available in `examples/requirements/brownfield_analytics.txt`.
The proposal begins by inspecting the existing URL creation, redirect, and analytics paths. The
implementation task depends on that inspection, followed by regression tests and documentation updates.
The engineer is instructed to check the proposed behavior against current contracts and tests.

### Ambiguous — vague performance and scale goals

**Input**

> Make URL redirects fast and scalable, and include analytics.

**Command**

```bash
python scripts/requirement_to_output.py \
  "Make URL redirects fast and scalable, and include analytics." \
  --scenario ambiguous
```

The same input is available in `examples/requirements/ambiguous_redirects.txt`.
The output asks for latency and traffic targets, analytics retention/privacy rules, and API/auth
expectations. It places clarification before design and implementation; the approval gate remains
closed.

## AI assistance, ownership, and validation

This CLI is deliberately transparent that its current runtime is deterministic rather than model-backed.
The assignment's AI-assisted development evidence is captured separately in the interaction log. For
each proposed task, the JSON identifies a way an AI coding assistant can help and a concrete engineer
validation responsibility. No proposal is applied automatically.

The analyzer has automated tests for all three scenarios, task dependency references, ambiguity
questions, empty input, and the command-line file-to-JSON path. Validate from the repository root:

```bash
pytest -v
ruff check .
```

For a real engineering change, additionally review the diff, run focused and full tests, inspect the
API schema, and exercise the URL-shortener smoke test against a running service.

## Limitations and risks

- Keyword matching is intentionally small and can miss context or infer the wrong scenario.
- The analyzer does not generate executable patches or call an AI model; `code_changes` are proposed
  file-level work items, not source code.
- API and schema proposals are starting points and must be checked against the existing project.
- “Scalable” is not a guarantee: actual capacity, deployment topology, and migration from SQLite require
  measurable targets and separate design/validation.
