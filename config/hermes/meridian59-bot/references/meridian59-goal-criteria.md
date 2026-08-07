# Meridian 59 goal criteria

Use this reference before submitting or accepting a durable goal. Verified against the controller implementation and a live broker snapshot on 2026-08-03.

## Validity has two layers

1. The object must match one supported criterion schema.
2. Its observation path or event kind must exist and measure the intended outcome.

Passing schema validation does not prove a criterion is satisfiable. Call `mcp__meridian_knowledge__validate_goal` before submission; it checks and canonicalizes static room references. The controller evaluates criteria against a broker observation shaped as:

```text
status.*
look.*
inventory.*
```

Do not copy paths from the public MCP status response. In particular, `game.*`, `goal.*`, and a root-level `vitals.*` are not criterion observation paths.

Schema validity also does not override durable campaign memory. `GOAL_DEFERRED`
means the controller has grouped the criteria with a prior failed outcome and
its observed retry predicate is still false. Changing ids, title, prose,
`after_cursor`, or the standard Tos Inn completion criteria is not a new goal.
Issue a supporting progression goal and retry only after `goal.retry_unlocked`.

## Verified observation paths

Prefer these stable paths for `state_equals`, `numeric_threshold`, and `numeric_delta`:

| Outcome | Path | Value type |
|---|---|---|
| Logged into the game | `status.in_game` | boolean |
| Character name | `status.character` | string |
| Current room name | `status.where.name` | string |
| Current room number | `status.where.num` | number |
| Current column/row | `status.position.col`, `status.position.row` | number |
| Current HP | `status.vitals.health.value` | number |
| Maximum HP / level | `status.vitals.health.max` | number |
| Current mana | `status.vitals.mana.value` | number |
| Maximum mana | `status.vitals.mana.max` | number |
| Current vigor | `status.vitals.vigor.value` | number |
| Vigor percentage | `status.vitals.vigor.pct` | number |
| Rested flag | `status.vitals.vigor.rested` | boolean |
| Karma | `status.karma.value` | number |
| Attribute value | `status.attributes.<attribute>.value` | number |
| Named skill ability | `ability.skill.<canonical name>` | number, 0-100 |
| Named spell ability | `ability.spell.<canonical name>` | number, 0-100 |
| Carried stack count | `inventory.items` via `inventory_contains` | criterion-specific |
| Current room | `look.room` via `location_reached` | criterion-specific |

Valid attributes currently include `agility`, `aim`, `intellect`, `might`, `mysticism`, and `stamina`. Fixed character attributes generally do not improve through play, so do not make progression goals from them.

Use `location_reached` instead of comparing `status.where.*` directly. Use `inventory_contains` instead of trying to index `inventory.items` with a dot path.

Named ability metrics are stable virtual metrics, not array paths. Copy the exact
`goal_metric` from `campaign.development` or
`progression_context.live_development`; the evaluator matches the current
server-derived `abilities.skills`/`abilities.spells` entry by canonical name.
The knowledge validator rejects unknown skill/spell names. Never use a list
index such as `abilities.spells.0.ability`.

## Optional Tos Inn bar finish

Include a return to the Tos Inn bar only when the user explicitly requests that
finishing location. Never add it as standing policy.

The authoritative map target is:

- room `52` (`RID_TOS_INN`); its current display name may misleadingly appear as `Familiars`
- column `8`, row `8`
- a live-verified walkable square beside the Tos innkeeper; `(8,7)` is occupied by bar objects

Use all three top-level criteria:

```json
{
  "id": "finish-in-tos-inn",
  "kind": "location_reached",
  "room_id": 52
}
```

```json
{
  "id": "finish-by-tos-bar-col",
  "kind": "state_equals",
  "path": "status.position.col",
  "value": 8
}
```

```json
{
  "id": "finish-by-tos-bar-row",
  "kind": "state_equals",
  "path": "status.position.row",
  "value": 8
}
```

Room 52 position `(8,8)` is the live-verified reachable standing square beside
Paddock and the stout; `(8,7)` is occupied by bar objects. Put the task outcome
criteria first and these home-position criteria last. Completion then requires
both the requested outcome and the character's return to the bar.

## Copy-ready criterion examples

Reach at least 25 maximum HP:

```json
{
  "id": "max-hp-25",
  "kind": "numeric_threshold",
  "metric": "status.vitals.health.max",
  "operator": ">=",
  "value": 25
}
```

Gain two maximum HP from a verified starting value of 23:

```json
{
  "id": "gain-two-max-hp",
  "kind": "numeric_delta",
  "metric": "status.vitals.health.max",
  "baseline": 23,
  "operator": ">=",
  "value": 2
}
```

Read the current status immediately before choosing a numeric-delta baseline. `numeric_delta` evaluates `current - baseline`; it does not capture the baseline automatically.

Raise the known Blink spell from its live current value to at least 10:

```json
{
  "id": "blink-10",
  "kind": "numeric_threshold",
  "metric": "ability.spell.Blink",
  "operator": ">=",
  "value": 10
}
```

Use `ability.skill.<canonical name>` for a skill. A threshold of `1` can verify
that a grounded new ability was actually learned. Always append the standard
home criteria, and do not issue training work unless live/grounded evidence
shows an executable practice or teacher path.

Paid teacher training is also a purchase. The goal must carry the funds contract
and verify the ability itself, not dialogue or arrival:

```json
{
  "request_id": "goal-learn-mace-fighting-unique-id",
  "title": "Learn mace fighting and return home",
  "objective": "Buy mace fighting training from Rook, verify the skill was acquired, then return to the Tos Inn bar.",
  "success_criteria": [
    {
      "id": "learn-mace-fighting",
      "kind": "numeric_threshold",
      "metric": "ability.skill.mace fighting",
      "operator": ">=",
      "value": 1
    },
    {"id":"finish-in-tos-inn","kind":"location_reached","room_id":52},
    {"id":"finish-by-tos-bar-col","kind":"state_equals","path":"status.position.col","value":8},
    {"id":"finish-by-tos-bar-row","kind":"state_equals","path":"status.position.row","value":8}
  ],
  "constraints": {
    "avoid_death": true,
    "bank_before_hazard": true,
    "purchase_plan": {
      "offering_kind": "skill",
      "item": "mace fighting",
      "merchant_class": "CorNothSergeant",
      "room_id": 154,
      "maximum_price": 500
    }
  },
  "priority": 70,
  "activation": "queue"
}
```

`maximum_price` is required and positive for skill/spell acquisition. It tells
the controller how much money must be carried before traveling: it visits Tos
bank room 54 and withdraws only the shortfall, then obtains a fresh teacher quote
and buys only the matching live id. A `conversation.responded` criterion, a
successful `say`, or `location_reached` at room 154 is never acquisition proof.

Reach Raza using its exact room number:

```json
{
  "id": "reach-raza",
  "kind": "location_reached",
  "room_id": 1012
}
```

The corpus contains two distinct rooms named Mausoleum. A bare `Mausoleum` is therefore invalid for goal submission. Resolve the intended room and use its numeric id, for example the Wilderness room:

```json
{
  "id": "reach-mausoleum",
  "kind": "location_reached",
  "location": "Mausoleum (Wilderness)",
  "room_id": 1006
}
```

Use `1016` for `Mausoleum (Raza)`. The knowledge validator rejects a conflicting name/id pair. The evaluator still performs case-insensitive name matching at completion time, but pre-submit validation requires a unique canonical entity.

Carry at least ten rubies:

```json
{
  "id": "ten-rubies",
  "kind": "inventory_contains",
  "item": "ruby",
  "count": 10
}
```

Verify the character remains logged in:

```json
{
  "id": "in-game",
  "kind": "state_equals",
  "path": "status.in_game",
  "value": true
}
```

Use `state_equals` for exact JSON equality. Do not use it for thresholds or room-name substring matching.

## Full goal patterns

### Buy an exact verified item

Purchase goals have a mandatory two-stage feasibility contract. Static validation proves that the item, merchant class, stock relation, and instantiated room agree. At execution time, the controller independently requires the merchant to be visible in that room and obtains a quote-only `shop` response before permitting any `buy_ids` mutation.

```json
{
  "request_id": "goal-buy-leather-armor-unique-id",
  "title": "Buy leather armor and return home",
  "objective": "Buy one leather armor from the verified Cor Noth merchant, then return to the Tos Inn bar.",
  "success_criteria": [
    {
      "id": "carry-leather-armor",
      "kind": "inventory_contains",
      "item": "leather armor",
      "count": 1
    },
    {"id":"finish-in-tos-inn","kind":"location_reached","room_id":52},
    {"id":"finish-by-tos-bar-col","kind":"state_equals","path":"status.position.col","value":8},
    {"id":"finish-by-tos-bar-row","kind":"state_equals","path":"status.position.row","value":8}
  ],
  "constraints": {
    "avoid_death": true,
    "bank_before_hazard": true,
    "purchase_plan": {
      "offering_kind": "item",
      "item": "leather armor",
      "merchant_class": "CorNothSergeant",
      "room_id": 154,
      "maximum_price": 1000
    }
  },
  "priority": 70,
  "activation": "queue"
}
```

This is a schema example, not a standing recommendation to spend 1000 shillings. Re-run knowledge validation against the current pinned corpus before use. Never substitute `TosBlacksmith`: it is source-defined but unplaced, and its null room is authoritative negative evidence. Do not use `item: "armor"`; the exact inventory criterion must match the exact planned item.

For a replacement weapon, count the broken carried copy. If the character already has
one broken mace, the acquisition criterion must require `"item": "mace",
"count": 2`; `count: 1` is already satisfied and will skip the purchase. After
the purchase, require `equip_best` in operator notes and do not resume a combat
goal until fresh status shows a non-empty `campaign.readiness.wielded_weapons`.

First call `mcp__meridian_knowledge__progression_context` with current max HP to choose evidence-backed prey and locations. Then train to 25 maximum HP and return to the Tos Inn bar:

Before copying this pattern, read `campaign_memory.combat_readiness` and
`combat_history`. Replace `25` with the smallest justified phase. After a death,
unknown equipment state, or unproven matchup, normally use current max HP plus
one; do not keep `25` merely because it is a round number. If the failed goal was
itself HP progression, use a bounded equipment, relevant trained skill/spell,
supplies, or money goal when that is the unmet retry predicate.

```json
{
  "request_id": "goal-hp25-tos-inn-unique-id",
  "title": "Train to 25 HP and return home",
  "objective": "Raise the character's maximum health to at least 25 through ordinary gameplay, then return to the Tos Inn bar as explicitly requested.",
  "success_criteria": [
    {
      "id": "max-hp-25",
      "kind": "numeric_threshold",
      "metric": "status.vitals.health.max",
      "operator": ">=",
      "value": 25
    },
    {
      "id": "finish-in-tos-inn",
      "kind": "location_reached",
      "room_id": 52
    },
    {
      "id": "finish-by-tos-bar-col",
      "kind": "state_equals",
      "path": "status.position.col",
      "value": 8
    },
    {
      "id": "finish-by-tos-bar-row",
      "kind": "state_equals",
      "path": "status.position.row",
      "value": 8
    }
  ],
  "constraints": {
    "avoid_death": true,
    "bank_before_hazard": true,
    "operator_notes": "Use the smallest grounded level gap and only a matchup supported by the character's verified equipped weapon and source vulnerability facts. Treat progression candidates as eligible, not proven safe. Call prey once for the unchanged state and hunting_grounds once for the selected creature; after both are grounded, launch or change tactics instead of repeating either read. Inspect the full room spawn risk. Recover fully and equip_best in a sanctuary; decide whether banking helps from live financial context. Resting stops at 80 vigor; if below the 100 combat floor, acquire verified edible food and let the keeper provision from sanctuary rather than telling the character to sit. Reagents count only when the known-spell list verifies create food. After swarm evidence, start safe-spot background farming from that sanctuary with flee_below=0.60, a 0.90+ hold-resume threshold, exactly 100 fight vigor, break_out_via_logoff=false, and an exact non-quarantined assigned room. Let the keeper own hazardous travel and combat; do not travel into the monster room first or issue foreground actions while it runs. A safe_spot.works label alone is not proof; rest damage, withdrawal, or a safe-spot failure message disproves it. Stop at the phase target and rerank prey when max HP reaches the prey's level."
  },
  "priority": 90,
  "activation": "replace_active_cancel"
}
```

Collect ten rubies and return to the Tos Inn bar:

```json
{
  "request_id": "goal-rubies-tos-inn-unique-id",
  "title": "Collect ten rubies and return",
  "objective": "Carry at least ten rubies, then return to the Tos Inn bar.",
  "success_criteria": [
    {
      "id": "ten-rubies",
      "kind": "inventory_contains",
      "item": "ruby",
      "count": 10
    },
    {
      "id": "finish-in-tos-inn",
      "kind": "location_reached",
      "room_id": 52
    },
    {
      "id": "finish-by-tos-bar-col",
      "kind": "state_equals",
      "path": "status.position.col",
      "value": 8
    },
    {
      "id": "finish-by-tos-bar-row",
      "kind": "state_equals",
      "path": "status.position.row",
      "value": 8
    }
  ],
  "constraints": {
    "avoid_death": true,
    "bank_before_hazard": true
  },
  "priority": 60,
  "activation": "queue"
}
```

## PvP: one fresh local opportunity

The ordinary client protocol cannot prove that another player died: after an accepted attack it can only observe that the player vanished, so the controller reports `target_left_or_defeated`. The specialized `pvp.phase.completed` event supplies correlated phase evidence: a fresh local target acquisition, at least one server-accepted swing, later disappearance, and a completed loot sweep. An empty sweep is evidence that looting was attempted, but it is not a property transaction; `property.transaction` is emitted only when an item was actually taken. Routine supervision may use this recipe only for an exact player present in fresh local visibility; it must never patrol to fill a daily quota.

Do not fabricate a confirmed-kill criterion, duplicate identical event criteria, or claim a verified kill. The same single event would satisfy duplicate criteria. Preserve killing and looting as the tactical intent and report the observed outcome honestly.

The controller anchors every submitted event criterion to the current durable event cursor. Do not guess a future round number and do not crawl the event log merely to calculate an anchor. You may omit `after_cursor`; the returned goal shows the authoritative value. Example first phase:

```json
{
  "request_id": "goal-pvp-opportunity-rival-unique-id",
  "title": "Take the fresh Rival opportunity and return home",
  "objective": "Engage the freshly locally visible player Rival, attempt to kill them, loot any property they leave, then return to the Tos Inn bar.",
  "success_criteria": [
    {
      "id": "pvp-phase-completed",
      "kind": "event_occurred",
      "event_kind": "pvp.phase.completed"
    },
    {
      "id": "finish-in-tos-inn",
      "kind": "location_reached",
      "room_id": 52
    },
    {
      "id": "finish-by-tos-bar-col",
      "kind": "state_equals",
      "path": "status.position.col",
      "value": 8
    },
    {
      "id": "finish-by-tos-bar-row",
      "kind": "state_equals",
      "path": "status.position.row",
      "value": 8
    }
  ],
  "constraints": {
    "avoid_death": true,
    "bank_before_hazard": false,
    "operator_notes": "Rival is present in the current fresh local observation. Use pvp_engage only against Rival; do not use who, pvp_seek, camp, patrol, or substitute another target if Rival leaves. Disengage to preserve the character's life when necessary. Loot autonomously; no approval is required."
  },
  "priority": 80,
  "activation": "queue"
}
```

This recipe does not misrepresent `target_left_or_defeated` as proof of death.
If the user requires an item actually to be taken, add `inventory_contains` for
that named item; do not use a generic property event as a proxy. A requested hunt
may use `pvp_seek` and a grounded multi-room route only when that search is part
of the explicit operator goal.

## Event criteria

`event_occurred` searches the controller's durable event stream for an exact event kind, scoped to the current goal. It cannot otherwise filter event data by target, item, or result. Before submission:

1. Confirm the exact event kind is emitted by the controller.
2. Omit `after_cursor` or supply the cursor you already have; submission replaces it with the current durable tail and reports `EVENT_CURSOR_ANCHORED` when it changed.
3. Never invent or round a future cursor. Legacy future cursors are clamped to the goal's own submission event rather than leaving the goal permanently blind.

Example: complete one future qualifying PvP phase:

```json
{
  "id": "future-pvp-phase",
  "kind": "event_occurred",
  "event_kind": "pvp.phase.completed"
}
```

The accepted future goal-event set is `pvp.phase.completed`, `property.transaction`, and `conversation.responded`. `pvp.engagement.completed` remains accepted only so the controller can migrate legacy PvP goals; do not use it in a new draft. Use an event only when any event of that kind is sufficient.

The controller does not emit `combat.kill`. For HP progression, max HP is the outcome: use `numeric_threshold` on `status.vitals.health.max` and the standard home criteria. Keeper kill counters are tactical progress evidence and must not be turned into an invented event criterion.

Never invent semantic game events such as `left_newbie_zone`. The controller does not currently emit that event. Represent travel with `location_reached` unless a verified dedicated event exists.

## Criteria to avoid by default

- `composite_all` and `composite_any`: wire-valid, but the current evaluator also requires every referenced top-level criterion individually. They do not currently provide useful AND/OR completion behavior.
- `operator_confirmed`: use only when the user explicitly wants manual evidence for a non-observable outcome. It is not permission to act and must not become an approval gate.
- Broad event kinds such as `action.succeeded` or `pvp.engagement.completed`: a non-qualifying event may satisfy them. Submission automatically supplies a fresh `after_cursor`.
- Paths guessed from the MCP dashboard, planner output, or prose documentation without verifying the broker observation.
- Unknown or ambiguous location names. `Silverfall` currently has no corpus match; do not submit it, retry it, or turn it into a spelling experiment.
