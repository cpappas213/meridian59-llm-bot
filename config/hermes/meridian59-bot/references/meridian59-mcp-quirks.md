# Meridian 59 MCP contracts and resolved quirks

Verified against the controller implementation on 2026-08-03.

## Current contract facts

### Persona

The persona schema is no longer opaque. Set accepts only:

- `name` (required)
- `character_voice`
- `traits`
- `speech_style`
- `values`
- `taboos`
- `relationship_defaults`
- `max_reply_characters`

Call `get`, then send its `version` as `expected_version` in a `set` request. Do not send top-level `version`. Unknown persona fields are rejected deliberately and the error lists the allowed fields.

The historical workaround “put persona in the goal objective” is retired. Persona is roleplay/conversation configuration; goals express game outcomes.

### Goals

Goal submission requires `request_id`, `objective`, and 1–20 typed `success_criteria`.

Supported criterion kinds:

- `state_equals`: `path`, `value`
- `numeric_threshold`: `metric`, `value`, optional `operator`
- `numeric_delta`: `metric`, `value`, `baseline`, optional `operator`
- `inventory_contains`: `item`, optional `count`
- `location_reached`: `location`, `room`, or `room_id`
- `event_occurred`: `event_kind`, optional `after_cursor`
- `composite_all`: `criteria` or `criterion_ids`
- `composite_any`: `criteria` or `criterion_ids`
- `operator_confirmed`: no additional required fields

Every criterion may have `id`. Fields belonging to another kind are rejected.

Allowed goal constraints are only `avoid_death`, `bank_before_hazard`, `operator_notes`, and `purchase_plan`. The last is the documented exact-offering/merchant-or-teacher/room purchase object; it supports `offering_kind` values `item`, `skill`, and `spell` and is not free-form metadata. Paid training requires a positive `maximum_price` and an exact named ability threshold at `>= 1`.

Equivalent failed outcomes are grouped by deterministic success criteria rather than title or prose alone. The standard Tos Inn bar finish, criterion ids, and event `after_cursor` values do not make a failed campaign outcome a new family. A goal-scoped lesson rejects direct submission and proposal acceptance with `GOAL_DEFERRED` until its observed retry predicate unlocks. A tactic-scoped lesson only suppresses the same failed tool, arguments, and location.

Status exposes these lessons under `campaign_memory.deferred_goals`, `campaign_memory.deferred_tactics`, `campaign_memory.eligible_retries`, and `campaign_memory.retries_in_progress`. Public lessons carry stable `id`, `goal_id`, `goal_family`, retry evaluation, prose `suggested_goals`, and the prior `original_goal`; new tactic lessons also carry exact `failed_tactic` tool/arguments/room. Do not act from a historical unlock event alone: match its lesson/family to a fresh eligible-retry status entry. A linked queued, active, or paused retry is in progress and must not be duplicated.

Direct submission also deduplicates equivalent active, queued, or paused goals and returns the existing canonical goal with a `GOAL_ALREADY_IN_PROGRESS` warning. Supervise or resume that id; do not paraphrase and resubmit it.

Goal-backed event criteria accept only `pvp.phase.completed`, `property.transaction`, and `conversation.responded`; `pvp.engagement.completed` is retained solely for legacy migration. `combat.kill` is not emitted and is rejected.

### Goal management

`expected_version` is recommended for every mutation. A `VERSION_CONFLICT` means status changed after it was read; refresh and decide again.

`confirm_complete` works only for a goal containing `operator_confirmed`, and only after all observable criteria are verified.

### Proposals

Proposals are inert future goals. Accept/reject requires `request_id` and `proposal_id`. Acceptance creates queued work; it does not execute a tactic or replace the active goal. Decision events include the supplied reason.

### Events

Start catch-up with `after_cursor: 0`. Continue from `next_cursor` while `has_more` is true. The maximum page size is 200.

### Knowledge grounding

`meridian_knowledge` is a separate read-only MCP server with exactly `search`,
`resolve`, `get`, `validate_goal`, and `progression_context`. It preserves the
six-tool `meridian_bot` supervisory contract. Goal validation returns a
`canonical_goal` but does not submit it; add a fresh `request_id` and submit that
canonical object through `meridian_bot`.

An exact `not_found` is negative evidence for the pinned corpus. An `ambiguous`
location requires selecting the intended candidate and using its numeric room
id. Do not retry spellings until one happens to pass.

## Error guide

| Error | Meaning | Next action |
|---|---|---|
| `unknown persona field(s)` | Persona contains unsupported keys | Use only the documented persona fields; do not guess prompt-like keys |
| `unsupported success criterion kind` | Criterion `kind` is not one of the nine supported values | Select the matching documented kind |
| `unknown <kind> criterion field(s)` | A field belongs to another criterion shape or is evaluation output such as `detail` | Use only fields listed for that kind |
| `success_criteria must contain at least one typed criterion` | Criteria is empty or missing `kind` | Provide 1–20 typed deterministic criteria |
| `VERSION_CONFLICT` | A versioned mutation used stale state | Refresh status and decide again |
| `INVALID_TRANSITION` | Requested goal/proposal state change is not legal from current state | Read current status and choose a legal transition |
| `MODEL_UNAVAILABLE` | Planner request failed or model output was invalid | Check diagnostic status and allow controller backoff |
| `BROKER_UNAVAILABLE` | Harness/game bridge is unavailable | Check diagnostic status and game connection; do not replay ambiguous mutations |
| MCP “unreachable” | Usually Hermes transport/process retry behavior, not a validation response | Wait for transport recovery; then make one read-only status call |
| `KNOWLEDGE_VALIDATION_FAILED` | Goal submission or proposal acceptance contains an unknown, ambiguous, or conflicting static reference | Read `details.errors`, resolve the intended entity, validate a corrected draft, then use a fresh request ID |
| `GOAL_DEFERRED` | An equivalent goal is known not to work in the current observed state, or a linked retry is already in progress | Read `details.lesson.retry_evaluation` and `details.lesson.suggested_goals`; issue a validated supporting goal when needed and retry only when fresh status lists the lesson under `eligible_retries` |
| `GOAL_ALREADY_IN_PROGRESS` warning | The submitted outcome already has one active, queued, or paused canonical goal | Use the returned goal id; supervise it or resume it when its disclosed predicate permits |
| `UNKNOWN_LOCATION` / `UNKNOWN_ROOM_ID` | No exact canonical entity exists in the pinned corpus | Choose a real search result or a different objective; do not repeat the same guess |
| `AMBIGUOUS_LOCATION` | More than one canonical room has that alias | Select the intended entity and send its numeric `room_id` |

`GOAL_DEFERRED` is HTTP/MCP conflict semantics but is not `retryable: true`: an unchanged retry cannot succeed. Do not evade it by changing title, wording, criterion ids, event cursor, or request id.

## Historical notes from initial tuning

The first live tuning session observed persona failures for `version`, `behavioral_rules`, `character_name`, `concept`, `language_tone`, `style_guidance`, `content`, `prompt`, and `systemPrompt`. Those keys remain invalid, but the supported schema is now documented, so trial-and-error and the old goal-objective workaround are unnecessary.

Several early malformed requests appeared alongside Hermes retry windows of roughly 10–60 seconds. Treat that timing as an observation about the Hermes transport, not a controller guarantee and not proof that validation errors stop the MCP server.
