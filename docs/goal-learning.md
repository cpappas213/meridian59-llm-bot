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
  materially revised tactic may be used. A new goal is needed only when the
  original strategic goal is no longer open.
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
| `insufficient_combat_power` | goal | max HP/mana increase, healing supplies increase, or a monotonic attributes/equipment/skills/abilities gain |
| `missing_capability` | goal | the same monotonic capability-profile gains |
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

Capability comparison uses semantic equipment identity (normalized item name
and slot), never a session-local object id. A reconnect that assigns a new id to
the same equipped weapon is therefore not retry evidence. Equipment loss after
death is also not an improvement: known equipment and structured capability
components must increase without a known loss. Aggregate capability hash
migrations are checked against those stored failed-state components so a
controller upgrade cannot manufacture an improvement.

Farm quarantine is the corresponding fast runtime gate. Crossing the configured
flee boundary once is an ordinary recovery event: the keeper retains control,
the phase remains active, and no lesson or quarantine is created. Two distinct
retreat/withdrawal episodes for the exact room, prey, and safe-spot strategy
inside 30 minutes quarantine that tactic. A death, lethal safe-spot failure,
depleted healing margin, or verified live over-level hazard still quarantines
immediately. An over-level hazard uses a specific retry predicate: max HP must
reach the level required by the configured danger margin, or the pinned source
corpus must change. It cannot unlock from a generic cooldown or unrelated
equipment-id churn. The same no-timeout rule applies once repeated retreats,
safe-spot failures, or depleted healing margin establish an exact farm
survivability failure. Other room/prey tactics remain available while that one
stays quarantined until readiness measurably improves.

Startup removes legacy quarantines whose sole evidence was an ordinary
flee-threshold crossing. Other survivability quarantines may be released after a
verified max-HP or equipment improvement. When the deterministic retry predicate
of an exact farm lesson unlocks, its matching runtime quarantine and retreat
counter are released in the same reconciliation step so the two gates cannot
contradict one another.

Progression research keeps `avoid_rooms` only as a diversity preference. It
prefers an exact room/prey tactic that recently completed a milestone, then an
eligible new room, and finally a recently considered room. Recent use by itself
never quarantines or rejects a tactic; only retained safety, stagnation, or
durable route evidence can do that. This lets finite hunting-ground tables reuse
proven farms across adjacent max-HP milestones without falsely exhausting every
candidate.

Every proposed farm room is also checked against its complete source spawn
table. A safe requested prey does not make a room valid when another
source-listed monster exceeds the character's danger limit. An exhausted
candidate set is retained as campaign evidence, not promoted into a
strategic-goal block. The controller re-evaluates recorded candidates after
lesson, quarantine, route, or survivability changes while preserving the
original outcome. A newly executable candidate can therefore become the next
phase without creating a replacement goal.

## Runtime behavior

At startup the controller backfills lessons from prior blocked/failed goals,
requeues every legacy controller-blocked strategic goal, and then evaluates all
deferred predicates against the fresh live observation. Each turn repeats that
repair and evaluation before planning.

Lessons are created when any bounded budget is exhausted. Ordinary action
failures count only when they are consecutive after the last verified action
success and occurred inside the configured evidence window; old or recovered
failures cannot accumulate forever:

- repeated semantic no-progress or broker action failure;
- repeating an exact known-failed action;
- ten consecutive planner waits by default;
- repeated retreat or death evidence for one exact combat/farm tactic; or
- an active legacy goal contains an invalid static reference.

Banking, routing, shopping, equipment, and evidence lookup are preparation
tactics. Exhausting their aggregate budget cannot by itself establish that the
campaign outcome is impossible; their lessons remain tactic-scoped. Startup
repairs legacy whole-goal lessons that were inferred from those failures.

A goal-scoped lesson never changes the original goal to `blocked`. While that
goal remains active, queued, or paused, an equivalent submission returns
structured `GOAL_ALREADY_OPEN` and points supervision back to the preserved
goal. If the original is no longer open and its retry predicate is still false,
the response is `GOAL_DEFERRED` (HTTP 409, `retryable: false`). A tactic-scoped
lesson suppresses only the matching tool, arguments, and room; the planner can
and should choose a different route or method.

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
- `campaign_memory.eligible_retries` (only when the original goal is no longer open)
- attention counts for deferred goals and eligible retries

Diagnostic status additionally includes raw public lesson records. Useful event
kinds are `goal.lesson.created`, `goal.reissue_suppressed`,
`goal.retry_unlocked`, `tactic.retry_unlocked`, `goal.retry.started`,
`goal.lesson.resolved`, and `action.lesson_suppressed`. Goal and tactic unlocks
use distinct event kinds. Repeated survival incidents emit planning evidence
but never block the strategic goal.

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
failure_evidence_window_seconds = 900
wait_budget = 10
survival_interrupt_budget = 3
world_retry_cooldown_seconds = 1800
generic_retry_cooldown_seconds = 3600
```

Changing budgets affects future classification. `survival_interrupt_budget`
controls only when the controller emits repeated-handoff planning telemetry; it
never authorizes a goal lifecycle transition. Existing lessons keep their
stored predicates and evidence.
