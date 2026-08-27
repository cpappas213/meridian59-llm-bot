# Long-horizon execution architecture

Status: normative controller design
Upstream boundary: `vendor/m59-harness` remains the protocol and mechanical layer

## Purpose

A strategic game outcome may take hours. The human or higher-level supervisor
defines that outcome; the configured LLM decomposes and executes it. Ordinary
problems such as recovery, inventory, commerce, equipment, supplies, travel, or
a failed farming tactic must not escape upward as replacement public goals.

## Principles

1. Strategic goals outlive tactics.
2. The configured LLM owns decomposition beneath an active operator goal.
3. The controller owns authority, persistence, and verified truth.
4. Model intent is not proof; observed semantic effects are proof.
5. Failure is contained to the smallest relevant action or phase by default.
6. Equivalent failures trip deterministic circuit breakers instead of hot loops.
7. Execution state survives controller, broker, MCP-host, and machine restarts.
8. No cheating is the hard play boundary; server rules remain operator policy.
9. Banking, equipment, and other preparation are tactics rather than universal
   gates.
10. Terminal locations, PvP, and progression targets exist only when the active
    goal or immediate defensive context calls for them.

## Responsibility boundaries

| Component | Owns | Does not own |
|---|---|---|
| Human/operator | LLM/game configuration, persona/name, goals, pauses/cancels, risk policy | Routine tactical approvals |
| Higher-level supervisor | Normalize/submit operator outcomes, report progress, surface genuine blockers | Inventing standing goals or tactical subgoals |
| Configured LLM campaign manager | Choose/revise internal phases, supporting work, and economic/progression tactics | Claim success or rewrite operator intent |
| Configured LLM action executor | Choose one permitted semantic action in the active phase | Bypass policy or mutate the game directly |
| Controller | Onboarding, persistence, reconciliation, capability selection, authorization, execution, verification, breakers | Invent persona or strategic goals |
| Harness broker/keeper | Protocol mechanics, pacing, movement, combat, bounded repetitive behavior | Product goals, supervisor state, completion |

## Onboarding boundary

Onboarding precedes this execution hierarchy:

1. the human configures an OpenAI-compatible endpoint/model;
2. the human supplies character name and persona;
3. the controller verifies that the selected live character has that name; and
4. a human or supervisor supplies the first strategic goal.

The controller has no character creation, suicide, reroll, or replacement
capability. Every selected identity is preserved; a mismatch requires the
operator to select/create the intended character outside the controller or
update the persona name. Onboarding does not imply a gameplay goal.

## Execution hierarchy

### Strategic goal

A public goal contains only its objective, observable terminal criteria,
constraints/operator intent, priority/provenance, and any explicitly requested
terminal position. It does not require a fixed prey, merchant, route, weapon,
bank visit, or farm recipe unless that tactic is itself the requested outcome.

```json
{
  "title": "Raise maximum HP to 45",
  "objective": "Raise the character's maximum HP to at least 45 through ordinary gameplay.",
  "success_criteria": [
    {
      "id": "max-hp-45",
      "kind": "numeric_threshold",
      "metric": "status.vitals.health.max",
      "operator": ">=",
      "value": 45
    }
  ],
  "constraints": {
    "avoid_death": true,
    "operator_notes": "Manage preparation and intermediate tactics internally. Do not cheat."
  }
}
```

### Campaign run

One durable campaign run records the goal/version, strategy summary, active
phase, phase stack, verified facts, open questions, rejected hypotheses,
progress checkpoint, failure counters, and any candidate external blocker.

### Phase

A phase is a bounded internal outcome such as research, readiness, freeing
capacity, liquidating loot, acquiring equipment, training an ability, farming a
local milestone, recovery, or an explicitly requested terminal return. A phase
has typed success/abandonment predicates, budgets, context, status, attempts,
failure evidence, and rationale. Supporting work is a child phase; the parent
resumes from fresh state after success.

### Action

The action executor sees the active phase, fresh observation, compact trajectory,
relevant knowledge, and only useful capabilities. It chooses one action and an
expected observable effect. The controller validates and serializes it.

## Model calls

- **Campaign manager:** invoked when a goal activates or a phase changes. It may
  start/replace a phase, push/resume support, report a completion candidate, or
  report an external-blocker candidate.
- **Action executor:** invoked for one action while a phase is controller-owned.
- **Failure reviewer:** summarizes evidence after bounded repeated failure and
  recommends continuing, replacing, or adding support.

Only deterministic controller code completes goals or classifies an external
blocker.

## Semantic verification

An RPC response is a receipt, not proof. Each mutation declares its verifier:

| Effect | Verification |
|---|---|
| Transfer/drop/sale/purchase | Matching property and currency deltas. |
| Bank operation | Carried/banked currency changes in the requested direction. |
| Equip | Equipment state contains the intended usable item. |
| Train | Exact skill/spell appears or its numeric value increases. |
| Travel | Room changes along the route or destination is reached. |
| Farm | Keeper ownership plus target/progress/withdrawal evidence. |
| Recover | Required vital or safe-location predicate changes. |
| Generic interaction | Declared state/event postcondition; otherwise unknown. |

Server refusal text is semantic failure even if a generic tool returned normally.
Unknown results are reconciled from fresh state before retry.

## Failure containment

An action signature includes capability, normalized intent/target, room,
relevant state hash, and expected effect. One corrected retry is allowed; the
second equivalent failure in unchanged state trips a breaker until its retry
predicate changes. Suppression produces one event followed by quiet backoff.

A phase is reconsidered after bounded breaker, progress, keeper, survival, or
assumption failures. Routine phase failure preserves the strategic goal.

A strategic goal becomes blocked only when evidence shows no grounded legal
path, a persistent required dependency outage, a missing required capability
without an ordinary alternative, a hard fair-play conflict, or exhausted safe
alternatives. One route, merchant, equipment, supply, inventory, or farm failure
is insufficient.

## Grounding

Tactical context joins static pinned knowledge, live ordinary-client state, and
learned evidence. It represents uncertainty and includes exact inventory,
equipment, currency, abilities, candidates, room risks, routes, quotes/refusals,
prior outcomes, and quarantine predicates relevant to the phase. Facts inform
the LLM; they do not prescribe an operator goal.

## Supervision behavior

The supervisor may submit goals, manage them at the operator's direction, review
future strategic proposals, and report evidence. It normally makes no mutation
while a goal is active. Full inventory, insufficient money, missing equipment,
a merchant refusal, route failure, keeper withdrawal, or failed farming tactic
is an internal execution issue rather than permission to replace the goal.

When no goal is active, the supervisor preserves explicit pauses and waits for
operator intent. It does not create a default progression campaign, PvP quota,
patrol, terminal destination, or character policy.

## Persistence and restart

SQLite stores campaign runs, phases, phase attempts, goals/transitions, lessons,
observations, action attempts, events, persona versions, onboarding state, and
leases. Startup acquires the singleton lease, connects the broker, observes the
character, reconciles onboarding/goals/phases, resolves already-satisfied typed
criteria, adopts valid keeper ownership, and only then resumes model work.

No game mutation is sent before reconciliation. Ambiguous in-flight attempts are
resolved from observation where possible and otherwise remain unknown with
operator-visible evidence.

## Invariants

1. At most one strategic goal is active.
2. At most one phase owns an action or keeper for that goal.
3. At most one game mutation is in flight per character.
4. Every mutation has goal/phase/attempt lineage or explicit survival lineage.
5. Every consequential action has a committed preflight before execution.
6. Model output cannot prove success or authorize cheating.
7. Equivalent failed signatures cannot execute indefinitely in unchanged state.
8. Internal failure does not silently rewrite strategic intent.
9. The controller never creates, suicides, rerolls, replaces, or recreates a
   character; onboarding is identity verification only.
10. The supervisor can explain the active phase, progress evidence, and next
    retry predicate from durable state.
