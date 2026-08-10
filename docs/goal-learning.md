# Durable goal-failure learning

The controller learns from bounded goal failures across goal IDs. This is a
controller-layer feature above `m59-harness`: the maintained broker still owns
ordinary game mechanics, while this project owns goal identity, evidence,
failure classification, retry gates, planner context, status, events, and
Obsidian projection.

## Why it exists

The tactical loop formerly remembered an exact no-progress call only for the
current goal ID. Cancelling and paraphrasing that goal erased the memory, and a
planner could repeat the same route, target, or over-difficult fight forever.
Durable lessons instead answer three questions:

1. What failed, with which evidence and character/world state?
2. Is the failure about the entire outcome or only one tactic?
3. What observed change makes a retry eligible?

The LLM may explain or propose around a lesson, but it never decides that a
retry predicate is satisfied.

## Stored lesson

SQLite table `goal_lessons` stores the originating goal/family, tactic key,
classification, `goal` or `tactic` scope, confidence, summary, failed-state
profile, evidence event IDs, deterministic `retry_when`, supporting-goal
suggestions, lifecycle timestamps, and retry/resolution lineage.

Statuses are:

- `deferred`: the predicate is false; an equivalent goal or exact tactic is
  suppressed according to scope.
- `unlocked`: a fresh ordinary-client observation satisfied the predicate and a
  materially revised retry may be submitted.
- `resolved`: verified success of the goal family resolved the lesson.

Goal-family identity is based on normalized deterministic outcome criteria. It
ignores titles, criterion IDs, `event_occurred.after_cursor`, and ancillary
finish-location/coordinate criteria. Those fields cannot be changed to evade a lesson.
At most one equivalent active, queued, or paused goal is retained. A direct
duplicate submission returns the existing canonical goal rather than creating
another retry.

## Failure classes and retry predicates

| Classification | Typical scope | Retry evidence |
|---|---|---|
| `insufficient_combat_power` | goal | max HP/mana increase, or attributes, equipment, skills, or abilities change |
| `missing_capability` | goal | the same capability-profile changes |
| `route_unavailable` | tactic | character changes room or the knowledge corpus changes |
| `world_unavailable` | goal | capability changes or the configured world cooldown elapses |
| `invalid_reference` | goal | knowledge corpus version changes |
| `ineffective_tactic` | tactic | capability changes or the generic cooldown elapses |
| `dependency_failure` | tactic | capability changes or the generic cooldown elapses |

Because every tactic key is room-scoped, any tactic lesson is also eligible
after the character changes rooms, including older lessons created before a
route-specific classifier was available.

PvP patrol routing uses the same rule with stronger evidence semantics. A
travel exception, unreachable hop, or requested/actual-room mismatch immediately
ends that patrol and records `route_unavailable` (or dependency failure for a
transport exception) at tactic scope. It preserves the requested room, actual
room, failed hop, route log, and broker reason. It is not target-not-found
evidence, completed acquisition coverage, or evidence that the character needs more
combat power. While the character and knowledge corpus are unchanged, the
controller suppresses cosmetic variations of a patrol that share the failed
location.

Combat readiness deliberately has no time-only unlock. If PvP or a hunt proved
too dangerous at 25 max HP, waiting an hour cannot make the same fight viable;
the character must measurably improve.

Farm quarantine is the corresponding fast runtime gate. For legacy
health-threshold quarantines that predate an explicit predicate, startup compares
the current character with the closest pre-quarantine combat evidence for the
same room and prey. A verified max-HP increase or equipment change releases that
survivability tactic for a bounded retry. Death evidence and structural failures
such as no usable safe spot, unreachable fight geometry, or a live over-level
hazard do not unlock merely because the character improved.

The operator-owned farm flee policy is also part of tactic identity. Lowering
that boundary from an older value releases only quarantines whose sole evidence
was reaching the former threshold without a withdrawal or death. It does not
erase a wall failure, structural hazard, withdrawal, or death record.

## Runtime behavior

At startup the controller backfills eligible prior blocked/failed goals, then
evaluates all deferred predicates against the fresh live observation. Each turn
it repeats that evaluation before planning.

Lessons are created when any bounded budget is exhausted:

- repeated semantic no-progress or broker action failure;
- repeating an exact known-failed action;
- ten consecutive planner waits by default;
- three critical-health/survival interrupts by default; or
- an active legacy goal contains an invalid static reference.

Banking, routing, shopping, equipment, and evidence lookup are preparation
tactics. Exhausting their aggregate budget cannot by itself establish that the
campaign outcome is impossible; their lessons remain tactic-scoped. Startup
repairs legacy whole-goal lessons that were inferred from those failures.

A goal-scoped lesson causes direct submissions and proposal acceptance to return
structured `GOAL_DEFERRED` (HTTP 409, `retryable: false`). The error includes the
public lesson, unmet condition details, and suggested supporting goals. A
tactic-scoped lesson suppresses only the matching tool, arguments, and room; the
planner can and should choose a different route or method.

Idempotent replay is checked before the learning gate, so replaying an identical
already-committed request returns its original result. A newly eligible retry is
linked through `retry_of_goal_id`. Verified success resolves all open lessons in
that goal family.

## Hermes, model, status, and events

Planner context contains `learned_failures` with current family lessons and
deferred tactics. It directs the model to avoid unchanged failed actions and to
pursue prerequisite progression instead of paraphrasing a goal.

The existing six-tool Hermes contract is unchanged. `status` adds:

- `campaign_memory.deferred_goals`
- `campaign_memory.deferred_tactics`
- `campaign_memory.eligible_retries`
- attention counts for deferred goals and eligible retries

Diagnostic status additionally includes raw public lesson records. Useful event
kinds are `goal.lesson.created`, `goal.reissue_suppressed`,
`goal.retry_unlocked`, `goal.retry.started`, `goal.lesson.resolved`, and
`action.lesson_suppressed`.

New lessons also expose `failed_tactic` (tool, arguments, and room) so Hermes and
the planner can distinguish an unchanged retry from a materially different
route or call. Older backfilled lessons may not have this field.

These interesting events flow through the existing notification pipeline as
evidence candidates. The local LLM normally combines a lesson and its surrounding
symptoms into one significance assessment instead of writing raw event lines.
Obsidian remains an informational projection; deleting or editing a note cannot
change campaign memory or unlock a retry.

## Configuration

Optional `[learning]` settings default to:

```toml
[learning]
enabled = true
no_progress_budget = 6
repeated_tactic_budget = 3
wait_budget = 10
survival_interrupt_budget = 3
world_retry_cooldown_seconds = 1800
generic_retry_cooldown_seconds = 3600
```

Changing budgets affects future classification. Existing lessons keep their
stored predicates and evidence.
