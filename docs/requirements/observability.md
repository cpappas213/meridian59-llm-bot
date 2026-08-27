# Observability, status, alerts, and journal

## 1. Outcomes

The operator must be able to answer, without waiting for the planner:

- Is the controller alive and is the character connected?
- What is the character doing, for which goal, and what changed recently?
- Is it safe, stuck, dead, executing a consequential tactic, or degraded?
- What evidence supports reported progress or completion?
- Are the broker, model, notifications, and journal healthy?
- What requires the user's attention?

Observability stores decisions and evidence, not private chain-of-thought.

## 2. Event model

The controller's event table is the source of truth. An event is committed in the
same database transaction as the state transition it describes when practical.
External alerts and journal writes consume committed events through independent
delivery cursors.

Required event families:

| Family | Examples |
|---|---|
| `runtime.*` | started, ready, degraded, recovered, stopping, crash-recovered, singleton-conflict |
| `dependency.*` | model unavailable/recovered, broker unavailable/recovered, server unavailable/reconnected, harness incompatible |
| `character.*` | joined, location milestone, vitals warning, near-death, death, respawn/recovery, inventory protected, alignment changed |
| `goal.*` | submitted, activated, progress, paused, blocked, resumed, succeeded, failed, cancelled, replaced, lesson created/resolved, retry unlocked/started/suppressed |
| `proposal.*` | created, accepted, rejected, expired |
| `consequence.*` | assessed, executed, abandoned, failed, outcome mismatch |
| `action.*` | prepared, sent, succeeded, failed, unknown, reconciled, policy denied |
| `combat.*` | encounter started, PvP started, escape, victory, defeat, notable loot |
| `economy.*` | bank deposit/withdrawal, major purchase/sale/trade, valuable item acquired/lost, theft outcome |
| `social.*` | noteworthy encounter, relationship milestone, conversation safety block |
| `model.*` | timeout, invalid output, retry exhausted, context pressure, recovered |
| `notification.*` | sink delivery failed/recovered, backlog threshold |
| `policy.*` | possible exploit, cheating rule denied, unexpected irreversible effect, configuration changed |

High-volume events such as every observation may remain `debug` and need not enter
the default event response or Obsidian journal.

## 3. Interesting-event policy

The following are `interesting=true` by default:

- goal activation, meaningful milestone, success, failure, block, cancellation,
  replacement, or material change of plan;
- creation or resolution of a durable failure lesson and deterministic retry
  unlock/suppression;
- a controller-created goal proposal;
- consequential-action preflight and verified outcome, including alignment
  changes, deliberate drops, and protected-property transactions (legacy
  reroll records remain audit history, but new rerolls are impossible);
- death, near-death, escape from a severe threat, or substantial loss;
- a PvP encounter and its outcome;
- successful or failed theft with material consequences;
- acquisition/loss of a rare, protected, unique, or high-value item;
- major bank, purchase, sale, or trade movement above configured thresholds;
- verified progression milestone, new ability, equipment tier, or important
  location discovery;
- alignment change or unexpected persistent character-state change;
- a socially significant interaction selected by the conversation/social-memory
  classifier, recorded as a summary rather than raw private conversation;
- controller/broker/model outage exceeding thresholds and subsequent recovery;
- suspected cheat/exploit or repeated policy denial; and
- notification/journal outage that risks the operator missing events.

Ordinary attacks, movement steps, routine loot, routine rest, planner calls, and
minor chat are not interesting by default. They remain queryable in structured
logs/events under retention policy.

## 4. Status freshness and health

Each status subsection includes its own observation timestamp/age. A green
controller heartbeat must not make stale game state appear current.

Suggested health evaluation:

| Component | Healthy | Degraded | Failed/blocked |
|---|---|---|---|
| Controller | lease + event loop heartbeat under 20s | loop delayed or optional worker down | lease lost, DB unavailable, state invariant failure |
| Broker | health responds and capability identity matches | transient call failures | unavailable/incompatible/duplicate suspected |
| Game | joined and observation age appropriate to mode | reconnecting or observation stale | auth rejected, persistent outage, unknown ownership |
| Model | recent request or health probe succeeds | elevated latency/invalid output | retry budget exhausted/unavailable |
| Journal | no delivery backlog | retrying below threshold | path invalid or backlog above threshold |
| Notifier | recent delivery succeeds | permission unavailable but journal works | repeated sink failure for critical alerts |

Status includes a top-level `as_of` time and reports `unknown` instead of carrying
forward a stale value without a flag.

## 5. Structured logs

Write newline-delimited JSON with:

```json
{
  "timestamp": "2026-08-03T18:36:12.530-07:00",
  "level": "INFO",
  "component": "executor",
  "event": "action_verified",
  "instance_id": "primary",
  "character_id": "redacted-stable-reference",
  "goal_id": "0198...",
  "correlation_id": "0198...",
  "action_kind": "bank",
  "duration_ms": 842,
  "result": "succeeded"
}
```

Rules:

- redact before serialization, not after writing;
- never log credentials, bearer tokens, complete environment blocks, raw prompts,
  private chain-of-thought, or raw account-auth packets;
- raw player tells are excluded from normal logs and kept only in a restricted,
  short-retention conversation record if enabled;
- use stable error codes and bounded field lengths;
- rotate by size/day and enforce retention; and
- record prompt/model metadata and hashes, not unrestricted prompt bodies, in
  production logs.

Recommended retention: 30 days for normal logs, 7 days for verbose debug traces,
90 days for controller events, and indefinite/high-level retention through the
user-owned Obsidian journal unless the user changes it.

## 6. Metrics

At minimum collect:

- process uptime and restart count;
- broker/game connection state and reconnect count;
- current observation age;
- active/queued/blocked goal counts and goal age;
- actions by kind/result, unknown results, policy denials, and action latency;
- non-progress counters and harness stall count;
- planner/responder calls, latency, prompt/output tokens, timeouts, invalid outputs,
  retries, and queue depth;
- current risk level, death and near-death count;
- pending proposal count, recent consequential-action count, and oldest age;
- event and notification delivery backlog/latency by sink;
- SQLite size, write latency, integrity result, and free disk space; and
- harness revision/capability manifest identity.

Metrics may be exposed as read-only Prometheus text on the LAN dashboard listener
or retained internally for the dashboard. Labels must avoid player chat, item
names with unbounded cardinality, and secrets.

## 7. Local desktop notifications

The MVP notifier uses Windows native notifications. It is independent of Hermes
Desktop so it works while Hermes is closed and does not require modifying Hermes.
The higher-level supervisor remains the place to inspect details and take action through MCP.

Notification content:

- title: short character/event summary;
- body: one- or two-sentence redacted result and requested action;
- stable event ID carried in metadata when supported;
- no credential, local path, raw private tell, or bearer token; and
- deep link only if a supported local dashboard/Hermes link is available without
  exposing mutation authority.

Routing defaults:

| Severity | Behavior |
|---|---|
| `debug` / `info` | Durable event only, except configured informational consequential events such as protected-property transactions, which alert once. |
| `notice` | Alert for `interesting=true`; coalesce similar events within 5 minutes. |
| `warning` | Alert promptly; repeat once if unresolved after configured interval. |
| `critical` | Alert immediately; repeat with rate limit until acknowledged or state recovers. |

Notification acknowledgement does not pause, cancel, alter, or retry a game
action. Notifications are informational and never part of execution control.

The notifier interface is versioned so a future supported Hermes-native delivery
sink can be added. The MVP must not call undocumented Hermes internals or inject
messages into an arbitrary active chat merely to create a toast.

## 8. Obsidian journal

### 8.1 Location

The controller receives the vault root explicitly from its private configuration
and writes, by default, to the vault's established active-project area:

```text
<vault>\01 Projects\Meridian 59 Bot\
  Meridian 59 Bot.md
  Journal\
    2026-08-03.md
    2026-08-04.md
```

`Meridian 59 Bot.md` is the human-facing executive campaign summary. It shows the
latest verified character, location, health, current goal/progress, semantic
liveness (including keeper activity or a repeated blocker), dependency health,
and newest milestone, followed by links to daily shards. Each
date-named file is an append-only shard for LLM milestone assessments whose
source events fall on that local calendar date. The sink shall not place bot
notes in `06 Daily`, which is reserved by the vault's existing generated briefing
workflow. The structured event database remains complete; Obsidian is
intentionally not a raw error or controller-operation log.

The controller creates only the configured project directory, index, `Journal`
subdirectory, and date shards if missing. It must resolve/canonicalize every
target and confirm it remains under the configured vault root before every open
after configuration reload.

### 8.2 Format

The project index is atomically refreshed after a delivered milestone and lists
daily shards newest-first:

```markdown
---
title: "Meridian 59 Bot"
date: "2026-08-04"
type: "meridian-59-bot-executive-summary"
---

# Meridian 59 Bot

## Current campaign

- **Character:** Sable
- **Location:** Tos Inn
- **Health:** 22/22 HP
- **Goal:** Raise max HP to 25 (active) — 25%
- **Risk:** safe
- **Liveness:** Active; keeper is farming; last verified progress 2 minutes ago
- **System:** No reported dependency failures

## Latest milestone

**Sable reached 22 max HP**

The farming plan produced one verified max-HP gain.

## Journal

- [[Journal/2026-08-04]]
- [[Journal/2026-08-03]]
```

The index is controller-owned. It refreshes atomically after milestone delivery,
shard creation/repair, and at a bounded healthy interval (one minute by default)
so current state remains useful even when no new milestone was journaled. That
refresh never appends a daily entry and therefore does not turn ordinary
controller activity into a log.

Each daily shard begins with:

```markdown
---
title: "Meridian 59 Bot Journal — 2026-08-03"
date: "2026-08-03"
type: "meridian-59-bot-journal"
project: "[[Meridian 59 Bot]]"
timezone: UTC
---

# Meridian 59 Bot Journal — 2026-08-03
```

Before calling the configured local model, the controller applies a deterministic
allowlist and durable deduplication key. Only these milestones pass:

- a verified max-HP increase;
- a newly learned skill or spell;
- a skill/spell crossing a five-point ability threshold;
- a goal becoming active, succeeding, failing, blocking, or pausing;
- a repeated identical controller safety blocker crossing the configured stall
  threshold (one assessment, not one line per suppressed action);
- a verified death;
- a completed PvP engagement; and
- an exceptional protected or valuable property transaction.

Startup, dependency flaps, proposals, retries, failure lessons, individual
safety suppressions, policy and consequence preflights, action errors, routine
bank/property activity, and other operational events remain in the event
database but never call the journal LLM.
The model writes one compact assessment of the new source milestone only. Passing
the deterministic allowlist makes the source journal-worthy; the model explains
it but cannot veto it. Current state and durable combat/lesson summaries explain
why that delta matters; they must not cause older developments to be recapped.

```markdown
<!-- m59-event:0198... -->
<!-- m59-event:0199... -->

## 2026-08-04 1:36:12 AM UTC — Sable made durable banking progress

Sable banked 480 shillings and advanced the active savings goal to 24%. This
was verified after the deposit rather than inferred from the attempted action.

**Why it matters:** The banked currency is protected from the next hazardous
outing and materially advances the campaign goal.

**What to watch next:** See whether the next plan uses or carries the new earnings effectively.

_LLM assessment · configured model · notice · 2 source events from
2026-08-04 1:35:58 AM UTC to 2026-08-04 1:36:12 AM UTC_
```

Entries derive both shard date and display time by converting RFC 3339 source
timestamps into the configured deployment timezone. They include the local date,
12-hour time, and timezone abbreviation. Markdown data is escaped so player or
model text cannot create frontmatter, embeds, HTML/script, or unintended links.

The assessor is given the milestone batch, current character state, active goal,
and compact campaign/combat summaries. It must distinguish observed facts from
uncertainty and may not invent causes or outcomes. If model assessment fails, the
source milestone remains queued; the journal never falls back to raw warning
lines.

### 8.3 Idempotency and concurrency

The delivery table has a unique `(sink, event_id)` key. For the file sink:

1. derive the shard date from `occurred_at` in the configured vault timezone;
2. group same-day candidates into a bounded assessment batch;
3. check the shard for every `<!-- m59-event:<id> -->` source marker;
4. append the complete escaped assessment and all source markers, then flush it;
5. atomically refresh the executive index from current state and canonical
   `Journal\yyyy-MM-dd.md` filenames, newest-first;
6. mark delivery successful; and
7. on crash recovery, scan for the marker before retrying and repair the index if
   its canonical link set is stale.

This avoids duplicate entries when the file write succeeded but the database
acknowledgement did not. It also avoids repeatedly rewriting an ever-growing file,
reducing Obsidian Sync and File Recovery churn. If another process has the shard
locked, queue/retry rather than overwriting it.

The controller database remains the event source of truth. The Obsidian project
is a derived, human-readable sink and must be rebuildable without community
plugins such as Dataview or Templater.

### 8.4 Privacy

Journal entries summarize why an interaction or other development mattered. They omit raw
private tells by default and never include credentials, system prompts, local
secret paths, or full model context. The user can explicitly enable selected raw
public chat later, but that is not an MVP default.

## 9. Controller dashboard

The read-only LAN dashboard contains:

- clear overall state and freshness;
- character vitals/location/risk;
- active goal, observable criteria, progress evidence, and current step;
- ordered queue;
- pending proposals and recent consequential actions without mutation controls;
- recent interesting events;
- dependency health and recent outages;
- model request rate/latency/errors and context use;
- broker/autopilot state and stall indicators; and
- controller/harness/prompt/config version identifiers.

It must visibly label stale/unknown data. It never displays credentials, raw
private chat, internal prompts, unrestricted action parameters, control secrets,
or local private paths. LAN access cannot cancel, submit, accept, or otherwise
mutate anything.

## 10. Alert deduplication and escalation

Deduplication keys combine event family, character, goal, and causal incident.
Recovery closes an incident and is itself interesting. Repeated symptoms update a
counter rather than producing one toast per retry.

Default incident thresholds:

- model/broker/game outage becomes `warning` after 2 minutes;
- outage becomes `critical` after 15 minutes;
- active goal non-progress becomes `warning` when its configured budget expires;
- a pending goal proposal is `notice` when created and summarized again only if it
  remains pending for 24 hours;
- notification/journal backlog is `warning` after 10 minutes or 100 events;
- disk free space is `warning` below 5 GiB and `critical` below 1 GiB; and
- suspected exploit, duplicate controller/broker, database integrity failure,
  death, or an unexpected high-impact irreversible change is `critical` immediately.

Thresholds are configuration and should be tuned after the first soak test.

## 11. Asking the supervisor for status

The supervisor should normally:

1. call `status(detail="summary")`;
2. mention any stale/degraded component and pending attention first;
3. summarize the active goal and verified progress;
4. state the character's location, risk, and last interesting event; and
5. call `events` or detailed status only when the user asks or the summary shows a
   problem.

The supervisor must distinguish verified facts from planner intent. “Planning to bank” is
not reported as “banked,” and a proposed goal is not reported as queued/active.
