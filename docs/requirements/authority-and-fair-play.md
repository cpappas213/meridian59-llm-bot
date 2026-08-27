# Authority, fair-play, and consequence guidance

## 1. Policy intent

The bot is allowed to play Meridian 59 as a real adversarial social game. There
is no PvE-only, no-stealing, no-deception, or “play nice” restriction. PvP,
theft, ambushes, bargains, lies, market play, rivalries, and other game-legal
tactics are valid when they advance the active goal.

The non-negotiable play boundary is **no cheating**. Death avoidance, banking,
preserving valuable property, and avoiding unnecessary alignment changes are
strong guidance rather than approval gates. Character suicide/replacement is a
separate hard capability denial: this controller never receives that authority.

**No game action requires operator approval.** A consequential action may be
performed autonomously after a non-blocking preflight. Hard denials exist only
for cheating, invalid/stale actions, unavailable capabilities (including all
character replacement), and failures that would make durable execution unsafe.

## 2. Authority order

From highest to lowest:

1. Hard no-cheating and credential-protection rules.
2. Durable user goals and explicit goal constraints supplied through the supervisor.
3. Configured account-protection and consequence guidance.
4. Controller survival/risk interrupts.
5. Planner-selected tactic.
6. Conversation/persona preferences.
7. Player speech and claims, which carry no operator authority.

When directives conflict, the higher level wins and the conflict is recorded.
Strong consequence guidance influences planning but does not create a wait state
for the user.

## 3. Operational definition of no cheating

For this system, an action is fair-play only when all of the following are true:

1. It is performed through the ordinary player protocol and a capability exposed
   by the maintained harness for normal character play.
2. It obeys server and broker pacing and does not attempt to gain advantage by
   packet flooding, malformed packets, replay, timing abuse, or parallel command
   races.
3. Movement respects world collision and reachable-path geometry even if the
   server fails to enforce a wall or distance check.
4. Decisions use information available through ordinary character observations,
   the bot's own durable history, player communication, or configured static game
   knowledge.
5. It does not use a server admin/maintenance interface, server process memory,
   server database/files, live spawn internals, another player's credentials,
   client memory injection, DLL hooking, or a custom proxy to reveal or forge
   hidden state.
6. It does not intentionally exploit a known game/server bug for an advantage.
   On discovering a suspected exploit, the controller stops that tactic, records
   evidence, and proposes reporting it upstream.
7. It does not coordinate extra accounts or processes outside the configured
   deployment to evade game limits.

Static maps, geometry, compendium entries, and spawn catalogs distributed by the
harness are treated as allowed durable game knowledge, comparable to a strategy
guide. This is an explicit project assumption. Live hidden server state remains
forbidden even if technically reachable on the LAN.

## 4. Action classes

### 4.1 Autonomous game actions

Subject to an active goal, risk checks, and ordinary game rules, the bot may
autonomously:

- connect to or select an existing character, reconnect, observe, wait, travel,
  rest, recover, flee, and use ordinary game services;
- fight creatures or players, initiate PvP, defend itself, pursue or disengage;
- steal, loot, conceal intent, bluff, deceive, negotiate, form or break tactical
  arrangements, and react to reputational consequences;
- buy, sell, trade, give away, drop, destroy, craft, train, equip, unequip, use, or
  consume items and currency;
- take actions that change alignment/karma when doing so serves the active goal;
- bank or withdraw currency/items for a goal;
- speak publicly or privately in the human-defined personality;
- explore and learn routes, creatures, markets, tactics, and relationships;
- choose, start, monitor, and stop bounded harness autopilot tactics; and
- propose new goals for later acceptance into the queue.

The presence of a permanent consequence does not change an action from
autonomous to permission-gated. It changes the required assessment, evidence,
and informational reporting.

### 4.2 Consequential actions

The following trigger a non-blocking consequence preflight and pre/post event:

- deliberate item/currency drop;
- sale, trade, giveaway, destruction, or other transfer of protected property;
- an action expected to change alignment/karma materially; and
- another capability classified as permanently consequential.

The controller strongly prefers avoiding unnecessary loss. It may still proceed
when the action serves the active goal and the expected benefit justifies the
consequence. It never pauses to request permission.

### 4.3 Always prohibited

- Any action failing the no-cheating definition.
- Using the server maintenance/admin socket for character play.
- Exposing credentials, secrets, internal prompts, private control endpoints, or
  control bearer material in game chat, dashboards, prompts, or logs.
- Accepting player speech as a goal, operator command, or policy change.
- Letting the LLM directly invoke unvalidated raw broker tools.
- Disabling, bypassing, or rewriting a hard policy denial after the model sees it.
- Concealing an executed consequential action or fabricating completion evidence.

## 5. Policy decision

Before execution, the authority engine receives a normalized action and current
verified state and returns one of `allow`, `allow_with_caution`, or `deny`:

```json
{
  "decision_id": "0198...",
  "decision": "allow_with_caution",
  "action_class": "protected_property_transaction",
  "matched_rules": ["GUIDE-PROPERTY-001"],
  "facts": {
    "item": "named weapon",
    "transaction": "trade",
    "estimated_value": 12000,
    "protected": true,
    "uncertainty": "low"
  },
  "summary": "The trade advances the active goal but permanently transfers a protected item.",
  "notify": true
}
```

`allow_with_caution` is an authorization to continue after recording the
consequence assessment; it does not wait for a person. `deny` is reserved for
hard no-cheating rules or execution-integrity failures such as an invalid action,
stale state, unavailable capability, or inability to durably record the attempt.

The policy decision itself is persisted before mutation. Inputs come from typed
action parameters and fresh observations—not from a free-form LLM rationale.

If an effect cannot be predicted reliably:

- uncertainty is recorded in the consequence preflight;
- the planner weighs the strongest plausible consequence and known alternatives;
- the action may still proceed if it serves the goal and is not cheating; and
- after an unexpected permanent change, the controller observes immediately,
  emits an event, and replans.

## 6. Consequence preflight

For each consequential action, record:

1. active goal and proposed action;
2. why it advances the goal;
3. expected permanent effects and uncertainty;
4. estimated item/currency/progression/alignment impact;
5. safer known alternatives and why they were not selected;
6. current survival urgency;
7. planned verification; and
8. the resulting `allow_with_caution` decision.

The preflight is deterministic where possible and may include a concise public
planner rationale. It is committed before execution, then linked to the verified
outcome. Notification delivery is asynchronous and does not delay the action.

## 7. Character identity and lifecycle boundary

- A human supplies the desired character name and persona; the controller does
  not invent an identity during execution.
- Onboarding observes the selected character and reports ready only after its
  live name matches the persona. It never selects a build or mutates a character.
- Missing, stale, cached, or conflicting identity evidence fails closed by
  withholding gameplay goals. It is never evidence that no character exists.
- Every differently named character, including a generated first-run placeholder,
  is preserved. The operator must select/create the intended character outside
  the controller or update the persona to match the selected character.
- The harness `reroll` tool is removed from controller capabilities and rejected
  at the planner, policy, broker call, and raw JSON-RPC boundaries. No action,
  persisted onboarding state, configuration value, or model output can override it.
- Broker capability growth is fail-closed: unreviewed tool names are unavailable,
  and lifecycle directives added under an approved tool name are filtered from
  planner enums and rejected again before network I/O.
- Harness `leave(forget=true)` is also rejected. Coordinated shutdown always logs
  out with literal `forget=false` and requires an explicit `forgotten=false`
  receipt.

These rules are hard authority boundaries, not operator approval prompts.

## 8. Alignment guidance

- Track current alignment/karma when observable and refresh it before a tactic
  known to affect it.
- Annotate relevant capabilities with expected direction, magnitude, and
  confidence.
- Strongly prefer avoiding incidental alignment change that does not help the
  active goal.
- When a tactic benefits from an alignment change, record a consequence preflight
  and allow it to proceed autonomously.
- Re-observe alignment after each material action and log the before/after value.
- Unexpected movement emits an interesting event and triggers replanning of the
  responsible tactic; it does not wait for user input.

This guidance is not a moral restriction on PvP or theft. It preserves awareness
of a persistent character attribute while leaving the bot autonomous.

## 9. Item and currency guidance

### 9.1 Protected property

The controller maintains a protected-property classifier based on:

- explicit operator allow/protect lists;
- unique, quest, soulbound, named, or irreplaceable flags when observable;
- estimated replacement cost and configured value threshold;
- scarcity and whether the item is the only functional equipment of its type;
- current/queued goal dependencies; and
- uncertainty: unknown high-impact items default to protected for reporting.

The bot may autonomously drop, sell, trade, give away, destroy, or consume any
property when doing so serves the active goal. Every transaction involving
protected or above-threshold property emits:

- an informational durable event before the action;
- a local desktop notification and Obsidian entry;
- the item/currency identity, quantity, estimated value, transaction type, goal,
  rationale, and uncertainty; and
- a verified outcome event showing the resulting owner/location/inventory state.

Routine low-value loot disposal and ordinary consumption remain in low-volume
logs and need not alert unless configured.

### 9.2 Bank-before-danger guidance

Before a planned hazardous phase, the planner receives financial context and:

1. classify the upcoming activity's death/robbery/loss risk;
2. observe carried currency and property;
3. determine what is required for the activity;
4. decides whether banking is useful given the plan, route, likely loss, and
   opportunity cost;
5. if it selects banking, travels to a bank and verifies the deposit; and
6. otherwise proceeds without treating carried wealth as a failed prerequisite.

Hazard triggers include planned PvP, difficult combat, unknown territory, a long
trip without safe exits, low supplies, recent near-death, hostile pursuit, or a
configured carried-value threshold. This is planner guidance, never a blocking
precondition and not a claim that all danger can be predicted. Running around
with any amount of cash remains legal controller behavior.

## 10. Death avoidance and survival interrupts

Avoiding preventable death outranks ordinary goal progress. The risk manager uses
fresh vitals, hostile count/strength, escape routes, effects, supplies, equipment,
location history, model/broker health, and recent damage trend.

Recommended states:

| Risk | Behavior |
|---|---|
| `low` | Continue goal. |
| `guarded` | Shorten action horizon, refresh observation, preserve escape route. |
| `high` | Stop initiating new conflict; rest, retreat, bank, resupply, or engage harness `survive`. |
| `critical` | Interrupt goal immediately and execute the safest available escape/recovery action. No LLM wait is required. |

Survival logic may select any fair-play game action, including a consequential
one, when that is the best available choice. The controller records the preflight
at the highest practical priority without waiting for operator input.

A death produces a critical event, captures the pre-death flight recorder,
reconciles post-death state, protects recovered value, and reevaluates the goal.
It does not automatically conceal the loss.

## 11. Social input and prompt injection

Every player message is untrusted data even if the player knows private details,
uses the user's name, claims to be the supervisor, quotes JSON/tool syntax, or says an
emergency exists.

Enforcement is architectural:

- chat enters only the responder's quoted message field;
- the responder has no tools;
- player/NPC chat content is never copied into planner, keeper, goal, policy, or
  gameplay context;
- command-like strings are never parsed as MCP/controller requests;
- only the authenticated loopback API can mutate goals/proposals/persona; and
- egress filtering prevents accidental system/control leakage.

The responder may choose to believe, disbelieve, joke about, or discuss a player's
in-world claim. That choice affects only speech; it cannot select or authorize a
gameplay tactic.

## 12. Persona authority

The human owns the creative personality description and may change it directly.
Persona affects wording, social posture, and roleplay choices. It does not:

- change the active goal or queue;
- turn player speech into trusted instruction;
- waive bank/survival/consequence guidance;
- permit cheating; or
- hide consequential actions from the event log.

If persona and goal conflict tactically, the planner may choose a goal-compatible
expression of the persona or report the conflict; it does not silently change
either durable object.

## 13. Policy configuration

Configurable values include item value threshold, bank threshold, hazard classes,
vital/risk thresholds, alignment significance threshold, event severity, and
protected item lists. Defaults are conservative for account preservation and
verbose reporting, but no consequence-guidance setting creates an action-approval
workflow.

Changing a policy threshold is an authenticated operator configuration action,
not a planner capability. Configuration changes are versioned, audited, and take
effect at the next policy check. Hard no-cheating rules are not configurable.
