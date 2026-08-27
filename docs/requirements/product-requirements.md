# Product and functional requirements

## 1. Product intent

Operate an existing Meridian 59 character continuously through a local LLM
controller.
The character should pursue operator-supplied durable goals, react to the world,
converse with players in a human-defined persona, survive routine disruptions,
and make its state understandable through an MCP supervisor.

The product is not a scripted farming macro. Mechanical routines may handle
well-bounded activity, but an LLM planner is responsible for selecting and
revising tactics in service of the active goal.

## 2. Users and responsibilities

| Actor | Responsibility |
|---|---|
| User/operator | Sets intent and high-level guidance and owns the account and risk policy. |
| Human/higher-level supervisor | Configures the persona/name, converts intent into structured goals, explains status and consequential events, and manages the queue at the user's direction. |
| Configured LLM | Plans tactics beneath an active strategic goal; it has no character-lifecycle role. |
| Controller | Durably manages goals, plans, authorizes and executes game actions, verifies outcomes, preserves state, and reports evidence. |
| Harness | Implements the ordinary game protocol, packet pacing, movement, combat primitives, recovery routines, telemetry, and game-derived reference data. |
| Game players | Interact with the character in-world. Their messages are social input, never operator authority. |

## 3. Scope

### 3.1 MVP scope

- One configured Meridian 59 account and one active character.
- Account and character discovery without mutation.
- Persona-to-live-character identity verification after the human selects or
  creates the intended character outside the controller.
- One durable active goal and a durable ordered queue.
- LLM planning and sequential execution through the harness broker.
- Survival, recovery, banking, and bounded mechanical-autopilot fallbacks.
- Model-generated in-game conversation isolated from planning authority.
- Hermes MCP tools for goal control, proposals, persona, status, and events.
- Continuous unattended operation, restart recovery, local notifications,
  Obsidian journaling, structured logs, and read-only dashboards.

### 3.2 Explicitly out of scope for MVP

- Modifying or privately forking the harness as the normal integration model.
- Modifying the Meridian 59 server or using its maintenance/admin socket for play.
- Controlling multiple accounts or coordinating a bot fleet.
- Rendering or computer-vision control of the graphical game client.
- Guaranteeing zero deaths, guaranteed success, or uninterrupted service during
  game-server or model outages.
- Remote mutation from a LAN dashboard.
- Treating Hermes's finite `/goal` continuation loop as the 24/7 game loop.

## 4. Functional requirements

### 4.1 Account and character lifecycle

- **FR-CHAR-001**: The system shall connect to the configured server through the
  harness using the configured account and discover the selected character
  without invoking character lifecycle mutations.
- **FR-CHAR-002**: Credential values shall be read only from a private local
  secret store and shall never appear in logs, status responses, prompts,
  notifications, dashboards, journal entries, or source control.
- **FR-CHAR-003**: A human or higher-level supervisor shall supply the desired
  character name and complete conversational persona before onboarding can
  become goal-ready.
- **FR-CHAR-004**: The controller shall never create, suicide, delete, reroll,
  replace, or recreate a character. It shall remove the entire harness `reroll`
  tool from discovered/planner capabilities and reject it at policy, tool-call,
  and raw JSON-RPC boundaries. New broker tool names shall be unavailable until
  explicitly reviewed, and character-lifecycle actions added beneath an approved
  tool name shall also be filtered and rejected.
- **FR-CHAR-005**: After joining, the controller shall capture a baseline character
  snapshot including identity, vitals, location, inventory/equipment summary,
  progress, bank state when observable, and alignment/karma state when observable.
- **FR-CHAR-006**: The controller shall not use the harness's destructive
  `leave(forget=true)` behavior under any circumstance. Every controller logout
  shall send the literal boolean `forget=false` and require an explicit
  `forgotten=false` broker receipt before shutdown may advance.
- **FR-CHAR-007**: On a fresh run, onboarding shall remain `awaiting_persona`
  until a persona is set and shall not invent a name, persona, or gameplay goal.
- **FR-CHAR-008**: A generated placeholder name matching `User` followed by
  digits shall receive exactly the same preservation as every other character.
- **FR-CHAR-009**: A differently named character shall always be preserved.
  Onboarding shall require external character selection/creation or a persona-name
  correction; no request, model output, configuration, or persisted legacy state
  may grant replacement authority.
- **FR-CHAR-010**: The controller shall expose durable onboarding status and
  shall not report `ready_for_goals` until the intended identity is verified.
- **FR-CHAR-011**: Goal submission and proposal acceptance shall fail with
  `ONBOARDING_REQUIRED` until onboarding reports `ready_for_goals=true`.
- **FR-CHAR-012**: The standard installer shall offer a local interactive path
  that collects and durably stores the operator-authored persona before launch,
  without requiring a supervising model or MCP host. Existing persona versions
  shall be preserved unless the operator explicitly requests an update.
- **FR-CHAR-013**: Model authentication shall be explicit and fail closed. The
  supported local modes are unauthenticated, HTTP Bearer API key for
  OpenAI/Codex-compatible providers, and Anthropic API key with the required API
  version header for Claude. Product subscription-login tokens shall not be
  imported or repurposed as application credentials.
- **FR-CHAR-014**: Interactive setup shall select timezone from labelled regions
  that persist valid IANA identifiers, normalize documented common aliases, and
  validate any advanced IANA entry before writing runtime configuration.
- **FR-CHAR-015**: An authenticated shutdown request shall immediately prevent
  new goal work, pause every runnable goal, serialize behind any in-flight game
  mutation, recover and route to freshly observed source-verified safety when
  necessary, stop the keeper, call `leave` only with `forget=false`, verify the
  character session is absent, and only then stop the controller and owned
  broker. Failure to verify any boundary shall leave the process running with
  goals paused and survival protection retained when joined.

### 4.2 Durable goal management

- **FR-GOAL-001**: The controller shall maintain at most one active goal and an
  ordered queue of zero or more goals.
- **FR-GOAL-002**: Goals shall persist across controller, broker, Hermes, and
  operating-system restarts.
- **FR-GOAL-003**: The higher-level supervisor shall be able to submit, list, inspect, pause, resume,
  cancel, replace, and reprioritize goals through a small MCP surface.
- **FR-GOAL-004**: A goal shall contain an objective, observable success criteria,
  constraints, priority, provenance, timestamps, and a durable status.
- **FR-GOAL-005**: The controller shall promote the highest-priority eligible
  queued goal when no active goal exists. Equal priorities shall retain queue
  order. After a bounded campaign phase succeeds at its verified safe ending
  and all keeper control is released, a strictly higher-priority queued goal
  shall cooperatively preempt the active goal. The interrupted goal shall be
  atomically requeued with its campaign state preserved so it resumes through
  ordinary scheduler promotion after the higher-priority work terminates.
- **FR-GOAL-006**: Pausing or cancelling an active goal shall first place the
  character in the safest practical stable state, unless an immediate survival
  action is already necessary.
- **FR-GOAL-007**: A replace operation shall atomically pause or cancel the old
  goal as requested and activate the replacement; a crash shall not leave both
  active.
- **FR-GOAL-008**: Goal completion shall require evidence satisfying each success
  criterion. An LLM assertion alone is not evidence.
- **FR-GOAL-009**: Controller-observed policy conflict, missing capability,
  repeated failure, death, or external outage shall not transition a strategic
  goal to `blocked`. The controller shall retain machine-readable evidence,
  stabilize immediate danger, suppress only disproved tactics, and preserve the
  goal for another tactic or supporting phase. An invalid contract or ambiguous
  mutation may be recoverably paused rather than guessed through.
- **FR-GOAL-010**: The controller may generate proposed goals from observed
  opportunities or risks. Proposals shall not become active until the supervisor or the
  user accepts them as queue-management decisions.
- **FR-GOAL-011**: Operator-supplied goal text is trusted intent, but shall still
  be normalized into the goal schema and checked against authority policy. In
  the local TUI, the configured model shall construct an inert structured draft
  from that text. The user shall approve, cancel, or revise it before submission;
  revision shall supply both the displayed draft and new operator text to the
  model and return to the same review step.
- **FR-GOAL-012**: Player-supplied chat shall never create, modify, accept,
  reprioritize, or cancel a goal.
- **FR-GOAL-013**: A bounded goal failure shall create a durable typed lesson
  containing evidence, failed character/world state, goal-or-tactic scope, and
  a deterministic retry predicate.
- **FR-GOAL-014**: While an equivalent strategic goal remains active, queued,
  or paused, the controller shall reject a duplicate retry and direct supervision
  to the preserved goal. If the original is no longer open, a goal-scoped retry
  remains ineligible until ordinary-client observation satisfies its predicate.
  Title, criterion-id, event-cursor, and ancillary finish-state changes shall not
  bypass equivalence.
- **FR-GOAL-015**: A tactic-scoped lesson shall suppress only the same failed
  action shape and shall not prevent a different tactic or supporting campaign
  goal.
- **FR-GOAL-016**: When the original goal is no longer open, an eligible retry
  shall link to it; an open original shall replan in place. Verified success
  shall resolve matching open lessons.
- **FR-GOAL-017**: The controller shall reject cancellation of a fresh active
  goal unless the operator explicitly requested it or evidence verifies a
  safety emergency, invalid goal, durable stall, or committed supersession.
  Replacing ordinary active work shall preserve it as paused by default.
- **FR-GOAL-018**: On startup and after each fresh broker observation, the
  controller shall requeue legacy controller-blocked goals, deterministically
  evaluate inactive goals, latch a verified complete typed criterion set without
  invoking the planning model, and mark it `succeeded` only when its previously
  model-selected safe ending is also verified. Otherwise it remains resumable
  for the required safe return.
- **FR-GOAL-019**: Controller-owned purchase and training flows shall perform
  post-acquisition travel or positioning only when the approved goal contains
  the corresponding location or coordinate success criteria. No city, inn, or
  square shall be an implicit goal criterion; the separate model-selected safe
  ending required by FR-GOAL-020 is completion hygiene, not goal scope.
- **FR-GOAL-020**: Every accepted execution plan shall declare an exact safe
  ending chosen by the configured planning model from source-grounded options.
  The controller shall reject a missing ending, an ending without
  `ROOM_SANCTUARY` or `ROOM_NO_COMBAT`, a non-final/non-travel ending step, or a
  mismatched runtime travel target. Once active campaign-phase criteria or public
  goal criteria are verified, the controller shall latch that outcome, permit
  only the ending step, freshly verify arrival and safety flags, and only then
  advance the campaign phase or set the goal `succeeded`.

### 4.3 Autonomous play

- **FR-PLAY-001**: While a goal is active, the controller shall repeatedly observe,
  plan, authorize, execute one broker action, verify the result, and journal it.
- **FR-PLAY-002**: All game actions shall pass through an attached harness broker;
  the controller shall not open an independent game-protocol connection.
- **FR-PLAY-003**: The controller shall respect the broker's pacing and shall not
  issue parallel game mutations for the same character.
- **FR-PLAY-004**: The planner may select any game-legal tactic, including PvP,
  theft, deception, trade, economic activity, grouping, travel, training, and
  social interaction, subject only to the authority and fair-play policy.
- **FR-PLAY-005**: The planner shall receive carried shillings and a best-effort,
  uncertainty-labeled inventory valuation so it can decide whether banking is a
  useful tactic. Carried wealth shall never be a deterministic travel or combat
  blocker.
- **FR-PLAY-006**: The controller shall prioritize avoiding preventable death and
  shall interrupt ordinary goal work for survival or recovery behavior when risk
  crosses configured thresholds.
- **FR-PLAY-007**: The system shall represent uncertainty honestly; "avoid death"
  is a priority and planning constraint, not a guarantee.
- **FR-PLAY-008**: A bounded harness autopilot may perform repetitive mechanical
  work selected by the planner, but the controller remains responsible for
  monitoring progress, detecting stalls, and changing tactics.
- **FR-PLAY-018**: A farm keeper shall launch only from a room whose pinned
  source facts include `ROOM_SANCTUARY` or `ROOM_NO_COMBAT`. The controller may
  reuse source-revalidated live staging history or select a connected/same-region
  safe candidate, but shall not infer a fixed home city from the farm region.
- **FR-PLAY-019**: Exhausting an internal phase action or time budget shall not
  strand the character in an unverified room. The controller shall durably latch
  the exhaustion boundary, prevent further goal work, retain or start survival
  control while exposed or recovering, and return to a source-verified
  `ROOM_SANCTUARY`/`ROOM_NO_COMBAT` room before marking the phase failed. Safety
  return actions shall remain audited but shall not consume the exhausted phase
  budget or create a goal-wide block.
- **FR-PLAY-020**: While a keeper owns urgent combat, pull, travel, retreat, or
  recovery work, controller observation shall first use broker-local keeper
  status and cached pushed look state. Current room and vitals shall override
  older status values, slower inventory/spell context shall be explicitly marked
  cached or unknown, and packet-paced full refreshes shall resume only after the
  urgent ownership boundary ends.
- **FR-PLAY-021**: The controller shall reconcile a durable keeper death receipt
  before any keeper-mode or goal-state early return. Each death shall be
  accounted once by a stable death identity, with a fresh Underworld transition
  or maximum-HP decrement as fallback evidence when the receipt is delayed.
  Recording a death shall not by itself invalidate a verified safe spot: damage
  or death from the monster already engaged or pulled there is compatible with
  the spot; only evidence that a new monster can acquire the character or an
  explicit placement/geometry failure may retire that coordinate.
- **FR-PLAY-022**: After bounded repair exhausts on a deterministic planner
  protocol, response-format, prompt-budget, or execution-plan failure, the
  controller shall durably suppress another equivalent model request for the
  same goal, phase, operation, plan, and material character/world fingerprint.
  The circuit shall survive controller restart and permit one bounded retry only
  after that fingerprint or planner operation changes.
- **FR-PLAY-012**: A PvP search shall visit multiple grounded public rooms and
  acquire an attack target only from a fresh local observation. A global online
  list shall not establish local attackability. The controller shall immediately
  engage a locally acquired target without a second model round trip.
- **FR-PLAY-013**: A completed PvP campaign phase shall require correlated,
  goal-scoped evidence of a locally acquired target, at least one server-accepted
  attack, later target disappearance, and a completed loot sweep.
- **FR-PLAY-014**: Before a PvP patrol, the controller shall use pinned
  source-derived effective room flags, including property-defined KOD class
  fields omitted by a flat zone export, to exclude no-combat and no-PK rooms,
  guild-only rooms without positively verified guild eligibility, and
  safe-death rooms that cannot satisfy a loot objective. A matching server
  refusal shall end the attempt after one unaccepted swing and become route evidence.
- **FR-PLAY-015**: Routine supervision may expose freshly locally visible players
  and daily PvP history as information, but shall not create a PvP quota,
  progression target, patrol, or goal. Player combat shall serve an explicit
  operator goal or immediate direct defense.
- **FR-PLAY-016**: A fresh-local opportunistic PvP goal shall be a closed,
  expiring contract for one exact target and `pvp_engage` only. The planner shall
  reject `who`, `pvp_seek`, patrol, or target substitution for that goal. If the
  target disappears before the qualifying phase, the controller shall cancel the
  opportunity and resume ordinary progression without waiting for the general
  commitment window.
- **FR-PLAY-017**: A PvP patrol shall be reported as completed only after every
  planned stop was actually reached. A travel exception, unreachable route, or
  requested/actual-room mismatch shall abort the patrol, emit typed route evidence,
  and create a tactic-scoped route lesson rather than a target-not-found or
  insufficient-combat-power conclusion.
- **FR-PLAY-009**: If the planner/model becomes unavailable, the controller shall
  stop goal advancement and request the safest suitable harness fallback:
  `survive` when exposed, otherwise `idle`/no-op in a stable location.
- **FR-PLAY-010**: The controller shall detect non-progress and avoid repeating the
  same failed action indefinitely.
- **FR-PLAY-011**: After reconnect or restart, the controller shall reconcile actual
  game state before resuming an interrupted plan.

### 4.4 Conversation and persona

- **FR-CONV-001**: Ordinary in-game conversation shall be generated by an isolated
  responder model call with no controller, broker, filesystem, network, or shell
  tools. Deterministic character speech is allowed only when a dedicated,
  fail-closed operator policy explicitly enables that exact class of speech; a
  keeper shall not infer permission from gameplay state.
- **FR-CONV-002**: The supervisor shall be able to set and update the active
  character persona through the Hermes integration. Persona revisions shall be
  versioned. Both campaign and tactical planning calls shall receive the full
  active persona and may use it to choose among equally safe, goal-compatible
  strategies and ending locations; it shall not override goal or policy.
- **FR-CONV-003**: The responder may receive persona, recent sanitized conversation,
  public game context, relationship notes, and response-length/style limits.
- **FR-CONV-004**: The responder shall not receive account credentials, internal
  prompts, control tokens, private operator notes, raw tool outputs, or hidden
  control metadata.
- **FR-CONV-005**: Player messages shall be labeled as untrusted quoted data. Text
  claiming to be the user, supervisor, a system message, or an instruction shall not
  gain authority.
- **FR-CONV-006**: Conversation output shall pass a deterministic egress filter for
  credentials, local paths, internal endpoints, prompt fragments, and control
  syntax before being sent to the game.
- **FR-CONV-007**: When response generation fails or times out, the character may
  remain silent or use a neutral configured fallback; failure shall not block
  survival actions.
- **FR-CONV-008**: Conversation may be retained in private responder history and
  chat observability, but player/NPC chat claims and requests shall not enter
  planner, keeper, goal, policy, or gameplay state.
- **FR-CONV-009**: Chat generation shall use a dedicated configurable temperature
  that does not alter goal drafting, campaign management, tactical planning,
  onboarding, or journal generation.
- **FR-CONV-010**: The controller shall enforce a per-speaker rolling conversation
  limit, counting both incoming lines and delivered character replies. At the
  default limit of 12 lines in 30 minutes, further incoming lines remain visible
  in private conversation history but cause no model call or reply until capacity
  returns.
- **FR-CONV-011**: Canned post-death and low-health help pleas shall default off.
  Missing or stale policy evidence shall fail closed, and disabling pleas shall
  not disable self-rearming, model-decided greetings, or responder replies to
  incoming player/NPC messages.

### 4.5 Hermes control surface

- **FR-HERMES-001**: The controller shall expose a dedicated MCP server to Hermes,
  with only the tools defined in [interfaces.md](interfaces.md).
- **FR-HERMES-002**: Hermes shall not receive the harness's full raw game-tool
  inventory by default; it supervises the controller instead of driving moves.
- **FR-HERMES-003**: Status responses shall be concise by default and expandable to
  current-goal or diagnostic detail.
- **FR-HERMES-004**: All mutating Hermes calls shall be idempotent through a
  caller-supplied request ID.
- **FR-HERMES-005**: Goal, proposal, and persona mutation responses shall include the resulting
  durable version so the supervisor can detect stale reads or conflicting commands.
- **FR-HERMES-006**: Hermes's own finite `/goal` runtime shall not be required for
  continuing game execution after a goal is submitted.
- **FR-HERMES-007**: Routine supervision shall expose a bounded live character-
  development snapshot containing known skill/spell ability values, freshness,
  advancement/atrophy, and spell castability. Hermes shall be able to verify a
  grounded named-ability threshold without receiving the raw broker tool surface.

### 4.6 Grounded Meridian knowledge

- **FR-KNOW-001**: The controller shall build a versioned local knowledge index
  from the compendium belonging to the pinned `m59-harness` revision without
  modifying or forking the harness.
- **FR-KNOW-002**: Indexed entities shall expose canonical names, aliases,
  structured facts, source references, source hashes, corpus version, and
  harness revision.
- **FR-KNOW-003**: Hermes shall receive a separate read-only
  `meridian_knowledge` MCP surface for search, exact resolution, entity detail,
  goal validation, and max-HP progression context.
- **FR-KNOW-004**: The tactical planner shall receive a bounded grounded context
  and a read-only search tool. It shall treat an exact zero-match as negative
  evidence and shall not invent game entities.
- **FR-KNOW-005**: Submission and proposal acceptance shall canonicalize known
  locations and reject unknown, conflicting, or ambiguous room references.
  Previously durable invalid goals shall be blocked before planning.
- **FR-KNOW-006**: Live ordinary-client observation shall override the static
  corpus for current state. Obsidian shall remain an informational projection,
  not an authoritative knowledge store.
- **FR-KNOW-007**: Corpus changes shall rebuild atomically and emit one
  interesting version-change event containing no source file contents or
  secrets.
- **FR-KNOW-008**: Connected progression context shall independently refresh
  live named abilities and spell readiness, and goal validation shall
  canonicalize named ability metrics against the pinned skill/spell corpus.

### 4.7 Consequential-action guidance and proposals

- **FR-GUIDE-001**: No authorized, available, structurally valid game action shall
  require operator approval. The controller shall hard-deny cheating, character
  lifecycle mutation, unreviewed broker capabilities, stale/invalid actions, and
  execution-integrity violations; remaining actions shall be evaluated
  autonomously under goal, risk, and consequence guidance.
- **FR-GUIDE-002**: Deliberate item drops, protected-property transfers or
  disposal, and alignment changes shall carry a strong default preference to
  avoid unnecessary permanent loss, but the preference shall never become an
  approval gate. Character replacement is instead governed by FR-CHAR-004's hard
  denial.
- **FR-GUIDE-003**: Before a consequential action, the controller shall record a
  non-blocking preflight containing the goal rationale, expected permanent effects,
  estimated value/loss, risk, uncertainty, and safer known alternatives.
- **FR-GUIDE-004**: The planner may proceed with a consequential action when it
  determines that the action serves the active goal and its benefits justify the
  consequences. The decision shall be evidence-backed and auditable.
- **FR-GUIDE-005**: Every transaction involving protected, unique, irreplaceable,
  quest-critical, or above-threshold property shall emit an informational event
  and configured desktop/Obsidian notification with before/after evidence.
- **FR-GUIDE-006**: An unexpected permanent consequence shall cause immediate
  observation, an informational or higher-severity event, and replanning. It
  shall not create a permission wait state.

### 4.8 Status, events, and notification

- **FR-OBS-001**: The controller shall expose current health, connection state,
  character summary, active goal, queue, last verified action, current risk,
  pending goal proposals, recent consequential actions, recent events, model
  state, broker state, and staleness.
- **FR-OBS-002**: Every meaningful state transition shall emit a structured event
  with a monotonic cursor and globally unique event ID.
- **FR-OBS-003**: Interesting events shall remain available to the configured
  desktop notifier, but the Obsidian assessor shall receive only deterministic
  executive milestones: max-HP gains, newly learned skills/spells, periodic
  ability thresholds, goal activation/outcomes, deaths, PvP outcomes, and
  exceptional protected/valuable property transactions. Each significant
  assessment shall be appended exactly once; routine operational events shall
  be marked suppressed without calling the journal model.
- **FR-OBS-004**: The Obsidian sink shall maintain a project-local current-campaign
  summary and append-only daily milestone shards. The summary shall show current
  character, health, location, active goal/progress, risk, dependency state, and
  latest milestone. Each journal entry shall include local source time, a compact
  LLM assessment of the new milestone only, and hidden idempotency markers.
- **FR-OBS-005**: The controller shall expose a read-only status dashboard that may
  bind to the LAN. It shall reveal no secrets or mutation controls.
- **FR-OBS-006**: Notification bursts shall be deduplicated and rate-limited without
  losing the underlying durable events.
- **FR-OBS-007**: A local authenticated terminal interface shall continuously
  display controller/game state, character vitals and development, current goal,
  goal queue, liveness, and recent events, and shall expose reviewed
  plain-language-to-structured goal submission plus versioned
  pause/resume/cancel/reprioritize/confirmation commands. It shall use color to
  distinguish operational states when the terminal supports ANSI output and
  provide an on-demand read-only view of all reported skills, spells, inventory
  quantities, carry capacity, attributes, and verified equipment. Escape shall
  immediately cancel any non-main screen or nested prompt and return to the main
  terminal dashboard without submitting a partial mutation.
- **FR-OBS-008**: The standard interactive launcher shall run setup only when no
  installed configuration exists. Subsequent launches shall attach the terminal
  interface to the independently running controller without creating a second
  game loop, and leaving the interface shall not stop the controller.

## 5. Non-functional requirements

- **NFR-REL-001**: Controller state shall use transactional durable storage. A
  crash at any point shall preserve a valid single-active-goal invariant.
- **NFR-REL-002**: Broker and controller shall each enforce a singleton lock for the
  configured character/account.
- **NFR-REL-003**: The deployment shall start automatically after boot without an
  interactive desktop session and shall restart failed processes with bounded
  exponential backoff.
- **NFR-REL-004**: The target is 24/7 availability, measured separately for
  controller process health, broker/game connectivity, and model availability.
- **NFR-PERF-001**: Interactive status calls shall complete within 2 seconds at the
  95th percentile without waiting for an LLM call.
- **NFR-PERF-002**: Goal mutations shall durably commit and return within 3 seconds
  at the 95th percentile under local healthy conditions.
- **NFR-PERF-003**: Planner inference may consume substantial GPU capacity, but
  controller loops shall have minimum cadences, timeouts, and retry budgets to
  prevent accidental unbounded request storms.
- **NFR-SEC-001**: Controller-owned mutation/control listeners shall bind only to
  loopback and require an authentication secret. The current harness broker,
  which has no authentication contract, shall remain loopback-only and be reached
  only by the expected same-user controller process; use upstream authentication
  if the maintained harness adds it.
- **NFR-SEC-002**: The LAN dashboard shall be read-only, redact player-private
  conversation by default, and contain no credentials, control tokens, raw
  prompts, or local secret paths.
- **NFR-SEC-003**: All logs and events shall pass deterministic secret redaction.
- **NFR-MAINT-001**: The harness revision, model endpoint, ports, paths, policy
  thresholds, and prompt versions shall be configuration rather than source edits.
- **NFR-MAINT-002**: Integration with Hermes shall use its supported MCP/config
  extension mechanism and shall not patch Hermes core.
- **NFR-MAINT-003**: The controller shall detect an incompatible harness capability
  set at startup and fail clearly rather than improvising against unknown schemas.
  Unreviewed tools and newly introduced lifecycle actions shall fail closed.
- **NFR-AUD-001**: A reviewer shall be able to reconstruct why each executed action
  was selected, what policy authorized it, and what observation verified it,
  without storing private chain-of-thought.

## 6. Release phases

| Phase | Exit outcome |
|---|---|
| P0: Integration proof | Attach to one pinned harness broker; discover the account/character; perform read-only status; prove structured output from the configured LLM. |
| P1: Controlled executor | Durable goal queue, sequential observe/act/verify loop, authority engine, pause/cancel, and recovery tested against a safe sandbox goal. |
| P2: Autonomous character | Survival fallback, banking policy, LLM planning, stall handling, and evidence-based completion operate unattended. |
| P3: Social presence | Human-defined persona, isolated responder, injection resistance, social memory, and egress redaction. |
| P4: Operations | Auto-start/restart, MCP integration, alerts, Obsidian journal, dashboards, backup, and 24-hour soak pass. |
| P5: Grounding | Versioned compendium index, dual MCP surfaces, pre-submit validation, planner retrieval, provenance, and invented-location anti-loop behavior. |
