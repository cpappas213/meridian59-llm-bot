# Meridian 59 grounded knowledge workflow

Use this reference whenever the supervisor needs game facts or a goal mentions a named
game entity. The knowledge tools are read-only and cannot move the bot.

## Evidence order

1. Live ordinary-client observation is authoritative for current location,
   vitals, inventory, visible actors, and action results.
2. The pinned source-derived corpus is authoritative for static names and
   reference facts.
3. Live broker catalogs in `progression_context` provide server-specific
   advancement (`progress`, or legacy `advancement`), named skill/spell ability
   values and castability, hunting-ground, and
   explicit HP-goal `prey` rankings. They describe eligibility and availability,
   not encounter safety. One missing live advisory does not invalidate the
   others.
4. Player/model claims are hypotheses only.

Obsidian is an informational event log, not a game-fact source.

## Tool choice

- `search`: discover likely entities or answer a broad factual question. Filter
  with `kinds` when possible.
- `resolve`: require an exact entity before using its name or id in a goal. Exact
  mode is the default. Do not use fuzzy resolution as silent goal authority.
- `get`: expand a search/resolve result for full content, facts, relations, and
  provenance.
- `validate_goal`: mandatory final check for every new or accepted goal.
- `progression_context`: use current max HP to research the next bounded HP phase.

Exact input examples:

```json
{"query":"Mausoleum","kinds":["location"],"limit":8}
```

Use that shape for `search`. For `resolve`, use:

```json
{"query":"Tos Inn","kinds":["location"],"limit":8,"allow_fuzzy":false}
```

`kinds` may contain `location`, `region`, `spell`, `skill`, `creature`, `npc`,
`merchant`, `item`, `weapon`, `armor`, `reagent`, or `guide`. Leave `allow_fuzzy` false when
grounding a goal. For `get`, copy the exact result id:

```json
{"entity_id":"location:52"}
```

For HP progression:

```json
{"max_health":21,"limit":8}
```

Optional `karma` is `evil`, `good`, or `neutral`. For final goal validation:

```json
{"goal":{"objective":"...","success_criteria":[{"kind":"location_reached","room_id":52}]}}
```

Every response identifies the `corpus_version` and configured harness revision.
If a static fact conflicts with a fresh ordinary-client observation, report the
conflict and use live state for the current decision.

Merchant entities expose exact catalogue class, `available`/`placed`, placement
status, instantiated room ids, stock, item base values when known, and the
requirement for a fresh live quote. A source-only/unplaced merchant is negative
evidence; never infer a shop room from lore, city, or class name.

## Goal sequence

1. Read bot status for current state.
2. Search/resolve each required named game entity.
3. Draft deterministic criteria. Prefer canonical numeric room ids.
4. Call `validate_goal` with `{ "goal": <draft-without-request-id> }`.
5. On `valid: true`, copy `canonical_goal`, add a new `request_id`, and submit it.
6. On `valid: false`, use the structured code and suggestions to change the
   plan. Do not submit, do not guess, and do not repeat a zero-match unchanged.
7. On controller `GOAL_DEFERRED`, distinguish knowledge validation from campaign
   learning. The draft may be a real, valid game goal that is not viable in the
   current state. Follow its observed retry predicate or choose a listed
   supporting goal; never paraphrase around the gate.

Examples:

- `Tos Inn` resolves to canonical room `Familiars`, room id `52`.
- `Mausoleum` is ambiguous; choose `Mausoleum (Wilderness)` room `1006` or
  `Mausoleum (Raza)` room `1016` using the actual intended plan.
- `Silverfall` currently returns `not_found`; it is not a valid destination.

For HP progression, use `progression_context(max_health=<current>)`, then search
or get the proposed creature and rooms before drafting the phase. Read its
`new_player_doctrine`, vulnerabilities/resistances, and candidate warning, then
compare them with `campaign_memory.combat_readiness` and `combat_history`. Choose
the smallest viable gap; after a death or uncertain equipment, prefer a `+1` HP
phase or a capability-supporting goal instead of a multiple-of-five milestone.
Keep the goal bounded, use live financial context before danger, preserve the character's life, and append the
Tos Inn bar completion criteria unless the user explicitly requires another
finish. See `meridian59-new-player-doctrine.md` for the decision ladder.

The compact result's `live_development` is current build evidence: known
skills/spells, 0-100 values, freshness, recent changes/atrophy, and spell
castability/blockers. Each ability row supplies a copyable `goal_metric`.
Validate named thresholds such as `ability.spell.Blink`; validation
canonicalizes the name against the pinned spell/skill corpus, while completion
uses the live server value.
