# Meridian knowledge system

The knowledge system grounds both decision layers without changing
`m59-harness`: the higher-level supervisor uses a small read-only MCP server to formulate
valid durable goals, while the continuous tactical planner receives a bounded
retrieval context and one read-only search tool each turn.

## Sources and authority

The static sources are the generated `vendor/m59-harness/compendium` and its
`substrate/m59-merchants.json` catalogue, both belonging to the configured
pinned harness revision. The builder indexes zone and spawn JSON plus creature,
NPC, merchant, spell, skill, item, weapon, armor, reagent, and guide pages.
Every returned entity carries its source path, SHA-256 source hash,
corpus version, and configured harness revision.

Use this evidence order:

1. ordinary live client observation for current room, inventory, vitals, visible
   actors, and the actual result of an action;
2. the source-derived corpus for stable game names and mechanics;
3. broker catalog tools for server-specific advancement, merchants, abilities,
   maps, and hunting-ground recommendations; and
4. player/model statements only as labelled claims that need verification.

Obsidian receives LLM assessments of significant corpus-version and validation
developments for human review. Routine validation mistakes may be suppressed
rather than written as raw warnings. It is not queried as a game-fact database
and a vault edit cannot alter the bot's beliefs.

## Build and update behavior

`KnowledgeBase` calculates a manifest over the relevant compendium files, index
schema version, and configured harness revision. At controller construction it
reuses `data/knowledge.sqlite3` when the manifest matches. Otherwise it builds a
temporary SQLite database, creates entity/alias/relation tables and FTS5 search,
then atomically replaces the old database. A crash cannot expose a half-built
index. The compendium remains read-only.

Advancing the harness pin or regenerating/changing a compendium file is enough
to trigger a rebuild on the next controller start. Status diagnostic detail and
`GET /v1/knowledge/metadata` show the resulting version, revision, source and
entity counts. The controller emits `knowledge.corpus.updated` once per new
corpus version; this becomes evidence for notification and Obsidian-assessment
pipelines.

## Runtime contracts

The planner can call `knowledge_search(query, kinds?, limit?)`. It cannot mutate
the index. Before storing a goal, accepting a proposal, or planning a legacy
active goal, the controller validates every `location_reached` criterion.
Canonical names and room ids replace aliases; name/id conflicts, unknown rooms,
and ambiguous locations fail. Common compact vital metrics such as
`vitals.health.max` are canonicalized to observation paths such as
`status.vitals.health.max`.

Named ability criteria use stable virtual metrics:
`ability.skill.<canonical name>` and `ability.spell.<canonical name>`. Goal
validation resolves the name against the pinned skill/spell corpus; completion
matches it against the fresh server-derived ability catalog. This avoids fragile
array indexes while keeping the broker itself private from Hermes.

For explicit item purchases and paid skill/spell acquisition, validation also
requires a structured `constraints.purchase_plan`. It resolves the exact
offering and merchant/teacher class, rejects source-only/unplaced merchants,
verifies the catalogue relation and numeric room, and requires the exact outcome
criterion: matching inventory for an item or a named ability threshold at `>= 1`
for training. Training also requires a positive price ceiling so the controller
can withdraw the shortfall before visiting the teacher. Static catalogues and
base values never prove current price or visibility; the controller obtains
fresh ordinary-client merchant and quote evidence in the room before buying.

An exact lookup with no matches is negative evidence for this corpus. The model
must change its target or gather new ordinary-client evidence. The controller
also suppresses repeated identical no-progress calls and blocks a legacy
ungrounded goal before it can execute an action.

Hermes uses the separate `meridian_knowledge` server:

- `search` for discovery and game research;
- `resolve` for exact names, aliases, class names, slugs, and room ids;
- `get` for full facts, relations, and provenance;
- `validate_goal` before every `meridian_bot.submit_goal`; and
- `progression_context` to decompose the 100+ max-HP campaign using the current
  character state and live broker recommendations. Its `compact` default retains
  complete spawn mixes and summarized safe-spot/combat/readiness evidence for
  routine goal selection; request `full` only for a specific ambiguity.

When supported by the pinned broker, the live section also includes `live_prey`:
the harness's source-derived ranking for an explicit HP advancement goal. It
adds stamina-aware HP ceiling, spawn composition, and advancement-yield evidence
without turning the ranking into an instruction to fight. The companion
`live_advancement` field is filled from the current broker's `progress` tool or
the legacy `advancement` tool. `live_development` combines the broker's current
0-100 skill/spell values, freshness, advancement/atrophy record, and spell
castability/blockers into a bounded goal-selection view. Routine controller
status exposes the corresponding snapshot as `campaign.development`. Each live
advisory is independent, so one
unavailable catalog cannot hide the other results.

Progression candidates are eligibility evidence, not safety recommendations.
The context also carries pinned new-player doctrine: max HP is level, guide-based
PvP protection lasts to 30 max HP unless live server evidence differs, death
drops the knapsack while stored property survives, equipment must be observed as
worn/wielded, and Underworld recovery may use any functioning portal followed by
overland travel. The controller combines these facts with empirical combat
history and exposes the result as `campaign_memory.combat_readiness`.

For a valid result the supervisor submits the returned `canonical_goal` after adding a
new `request_id`. `valid: false` is a planning correction, not a reason to guess
another spelling or loop on the same place.
