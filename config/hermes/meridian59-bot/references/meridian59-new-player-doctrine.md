# Meridian 59 new-player and combat doctrine

Use this reference when supervising HP progression, equipment readiness, PvP,
death recovery, or repeated combat failure. The tactical controller applies
hard per-action safety normalization; Hermes chooses the bounded strategic goal.

## Evidence hierarchy

1. Fresh ordinary-client observations and completed combat outcomes.
2. Controller `campaign_memory.combat_readiness`, `combat_history`, and lessons.
3. Pinned source-derived game facts and live broker catalogs.
4. Model inference, which must remain explicit and conservative.

`progression_context.candidates` means only that a creature is above current max
HP and can be relevant to the game's max-HP roll. It does not establish that the
creature, its room, or its full spawn mix is survivable.

Within readiness, `farm_tactic_quarantines` and `recent_farm_evidence` are live
empirical overrides. A source-eligible prey in a quarantined room is not a valid
retry until a materially different tactic is chosen.

## Grounded mechanics

- Maximum HP is character level. A kill only rolls for max-HP progression when
  the victim's level is above the character's current max HP.
- Official new-player guidance uses 30 max HP as the end of PvP protection.
  Unless fresh live evidence proves this server differs, do not schedule PvP
  below 30.
- Room flags determine whether a PvP phase is executable. `ROOM_NO_COMBAT` and
  `ROOM_NO_PK` prohibit player attacks. `ROOM_GUILD_PK_ONLY` permits city combat
  only with verified guild eligibility; do not infer membership from max HP or
  the global player list. `ROOM_SAFE_DEATH` allows combat but cannot satisfy a
  progression-and-loot hunt. The controller enforces these facts before travel.
- Death drops the knapsack; banked or stored property survives. Bank carried
  currency before danger and treat a death as a capability failure, not a cue
  to retry the same fight.
- Vigor ranges from 1 to 200, and sitting restores it to 80. That natural rested
  threshold is sufficient for the ordinary keeper launch policy. The keeper uses
  carried or self-created food as optional endurance by default, but does not buy it
  unless the plan opts in; food must not become a financial prerequisite for combat.
  Reagents are a usable provisioning path only when live spell evidence confirms that
  the character knows `create food`; carrying elderberries and herbs does not prove that.
- Carrying a weapon or armor is not evidence it is worn or wielded. When
  `equipment_state` is `known`, the controller's `equipped` list comes from the
  server's separate equipment state and is authoritative. Otherwise equipment is
  unknown, even when the pack contains weapons or armor.
- Skill and spell ability values are durable server-derived progression evidence.
  Use their advancement/atrophy history to justify training goals; possession of a
  skill or spell name alone does not show improvement.
- Learning an unknown paid skill or spell uses the merchant shop transaction.
  A teacher visit or dialogue is not training evidence. Ground one exact ability,
  teacher class, room, and positive price ceiling; require the exact named ability
  metric at `>= 1`. The controller will withdraw the authorized shortfall at Tos
  bank before traveling to the teacher, then bind the purchase to a fresh quote.
- `campaign.development` and `progression_context.live_development` expose those
  current 0-100 values plus freshness and spell castability. Spells improve by
  successful casts. Weapon proficiencies and strokes improve through ordinary
  attacks with the matching weapon; other skills in this fork are passive and
  cannot be invoked directly. Nontrivial targets teach better, while
  `ROOM_HARD_LEARN` divides learning chance by ten. Verify a bounded phase with
  `ability.skill.<canonical name>` or `ability.spell.<canonical name>` rather
  than an array index or a claimed practice count.
- A normal armor set spans hands, pants, shield, and body. Missing or unverified
  slots justify a bounded equipment/supplies goal after dangerous failure.
- Return portals lead to different towns. If the preferred Underworld portal is
  unreachable, use any functioning reachable portal and travel overland rather
  than varying fine movement or step limits around the same blocked coordinate.

Sources embedded in controller knowledge:

- https://www.meridian59.com/guides/getting-started/
- https://www.meridian59.com/guides/skills-and-spells/
- https://www.meridian59.com/guides/your-goods/
- https://meridian59.wiki.gg/wiki/How_to_increase_your_Max_HP_%28i.e._how_to_level_up%29

## Goal-selection ladder

Choose the first applicable class:

1. Recover from the Underworld, restore full vitals/vigor, and bank money.
2. If below 30 max HP, defer PvP and work toward 30.
3. If equipment state is unknown, armor is absent, or a recent same-tier fight
   caused death/critical disengagement, issue one bounded equipment, relevant
   trained skill/spell, supplies, or money goal that changes a retry predicate.
4. Otherwise issue the smallest safe HP phase, normally `+1` after a failure.
5. Increase to `+2` or `+3` only after empirical safe outcomes against the same
   target or a better-grounded matchup. Do not jump to a multiple of five merely
   because it is a neat milestone.
6. At 30+, continue bounded HP phases by default. Interrupt progression for one
   opportunistic PvP phase only when another player appears in fresh local
   visibility; a separate second natural encounter is allowed, but never patrol
   or queue PvP to fill the two-per-day initiation cap.

Return to room 52, column 8, row 8 at the Tos Inn bar only when the human's goal
explicitly requires that finish. The adjacent `(8,7)` square is occupied by bar
objects and is not a valid finish. Farm launch staging is independent of that
optional destination: select a room whose source facts include
`ROOM_SANCTUARY` or `ROOM_NO_COMBAT`, prefer one actually observed and remembered
as safe, and never infer a home city from the farm region.

## Combat goal notes

Keep goals outcome-based. In `constraints.operator_notes`, identify the evidence
that makes the phase reasonable: intended prey/location if grounded, smallest
level gap, favorable vulnerability versus the character's verified weapon, known room
spawn risk, and a recovery fallback. The controller will force unproven `fight`
calls to one round and one swing, equip automatically, reobserve after each call,
and refuse new combat until full health/mana/rest, banking, and recovery checks
pass. Do not encode broker call sequences as success criteria.

When choosing between otherwise similar level-30 creatures, use source facts and
the character's actual weapon: for example, a giant rat's fire/bludgeon vulnerability is
useful only if the character has a verified compatible attack. It is not a universal
claim that giant rats are safe, and it does not make a mixed-spawn room safe.

Treat `prey` and `hunting_grounds` as one-shot planning evidence for an unchanged
state. After one successful prey ranking, select the creature and inspect its
hunting grounds once. After one grounded room result, launch the bounded keeper
from sanctuary or choose a materially different room. Repeating either lookup
does not improve readiness and is a planning loop.

After `character.died` or a controller farm-survival handoff, do not reissue the
failed goal. Follow the resulting goal-scoped `insufficient_combat_power` lesson.
A linked paused retry remains in `campaign_memory.retries_in_progress` even when
root `goal` and `queue` are empty. Do not duplicate or resume it until status
shows its deterministic capability predicate unlocked.

Healing supplies count as a capability change only when the controller observes
the carried item. The source-derived `Flask` restores 5-10 HP and is consumed on
use. After a critical escape or death, replenish to at least four verified
Flasks; fresh status must satisfy the lesson's `numeric_at_least` predicate before
Hermes retries the failed combat family.

Treat a keeper safe spot as provisional until it holds for multiple live passes
with an adjacent attacker. A `safe_spot.works` label based on a short or
attack-free test is not enough. Ordinary retaliation while resting, forced
withdrawal, or death from the monster already engaged or pulled to the wall does
not disprove the spot. Retire the exact coordinate only when live evidence shows
a new, previously unengaged monster can acquire the character there, or an
explicit placement/geometry failure is reported. Do not reuse a room listed in
`farm_tactic_quarantines`; select another grounded room with a viable full spawn
mix and begin from a sanctuary.
