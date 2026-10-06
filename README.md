# Agent Realm reference agents

Example agents that play [Agent Realm](https://agentrealm.gg) through its public HTTP API. Clone this repo, point it at your account, and a character starts moving in the world. They are ordinary clients: they import nothing from the server and never touch its databases. The agent lives in [`python/`](python/): Python 3.11+, standard library only, nothing to host; the one install is the AI planner's `anthropic` SDK (`make setup`), which `make test` and `--no-planner` do not need.

More depth: [`PLAN.md`](PLAN.md) (design and backlog), [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md) (character files, every state, directives, metrics), [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md) (game facts), [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md) (playable-agent milestones), [`AGENTS.md`](AGENTS.md) (rules for agents that build this repo). The site’s [guides](https://agentrealm.gg/guides) and [docs](https://agentrealm.gg/docs) describe the API itself.

## Quick start

You need an [agentrealm.gg](https://agentrealm.gg) account and an API key from your account page.

```sh
git clone https://github.com/evandelacruz/agentrealm-agents.git
cd agentrealm-agents
export AGENTREALM_API_KEY=...   # from your account page

cd python
python3 -m agentrealm_agent create characters/wren.toml --name Wren
python3 -m agentrealm_agent run characters/wren.toml --character-id ID   # ID from create
```

Other commands:

```sh
python3 -m agentrealm_agent run characters/wren.toml --character-name Wren
python3 -m agentrealm_agent status characters/wren.toml --character-id ID
python3 -m agentrealm_agent metrics characters/wren.toml --character-id ID   # or: metrics path/to/trace.jsonl (or a .json snapshot)
python3 -m agentrealm_agent compare-metrics baseline.trace.jsonl candidate.trace.jsonl
```

The default API is `https://api.agentrealm.gg` (`AGENTREALM_BASE_URL` overrides it). Sample profiles use `world = "sandbox"` (free practice); set another world code to play live, where lives are permanent. `create` prints a character id. `run` and `status` pick the character from `--character-id` or `--character-name`, else `AGENTREALM_CHARACTER_ID`; an explicit flag always overrides the environment. `run` plays until Ctrl-C and appends a trace under `python/.state/<profile>.<character_id>.trace.jsonl`. A trace from before A59 (`<profile>.trace.jsonl`) is left as is: pass its path to `metrics`, or rename it to `<profile>.<character_id>.trace.jsonl` so new runs append to it. Run one `run` process per world at a time.

### The AI planner

`run` plays with the AI planner (A35), which owns the goal stack. Give it a key before you run:

```bash
make setup                            # pip install -r python/requirements.txt (anthropic); the rest is standard library
export AGENTREALM_PLANNER_ANTHROPIC_KEY=...   # or ANTHROPIC_API_KEY; default provider, model claude-sonnet-5-5
python3 -m agentrealm_agent run characters/wren.toml --character-id ID

# or OpenAI (no extra package):
export AGENTREALM_PLANNER_PROVIDER=openai AGENTREALM_PLANNER_OPENAI_KEY=... AGENTREALM_PLANNER_MODEL=...
```

Keys are read from `AGENTREALM_PLANNER_ANTHROPIC_KEY`, then `ANTHROPIC_API_KEY`, and from `AGENTREALM_PLANNER_OPENAI_KEY`, then `OPENAI_API_KEY`: the planner's own names come first because some hosts (Claude Code cloud sessions, for one) strip the standard ones. With no key, `run` stops at startup with one line saying which key to set. Before play it makes one check call to the provider; a key the provider refuses (HTTP 401 or 403), or a model it does not know (any other 4xx but 429), stops `run` and every smoke script with exit 2 and one line. A call that fails mid-run keeps the stack and backs off: 2 s, doubling with each failure in a row, at most 120 s. `AGENTREALM_PLANNER_MODEL` picks another model. `run --no-planner` plays without it, from the character file's `policy.goals`; that is a test mode, not how the agent is meant to play. The smoke scripts take the same `--no-planner`. Never commit a key. Moving from the old strategist settings: `AGENTREALM_STRATEGIST_MODEL` is now `AGENTREALM_PLANNER_MODEL`, `AGENTREALM_STRATEGIST_API_KEY` is gone (use the provider keys above), and the per-run limits (`AGENTREALM_STRATEGIST_MAX_CALLS`, `_MAX_TOKENS`, `_MIN_INTERVAL_S`) are replaced by `AGENTREALM_PLANNER_CALLS_PER_MIN` and `_TOKENS_PER_MIN`; `AGENTREALM_STRATEGIST_IDLE_MINUTES` is `AGENTREALM_PLANNER_IDLE_MINUTES`. Cadence, budget and triggers: [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md) **Strategist**. More in [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md) and [`PLAN.md`](PLAN.md) **CLI**.

The planner plays with the game's own documentation in its prompt (A62): the site's docs and guides, saved in [`python/agentrealm_agent/reference/agentrealm_reference.md`](python/agentrealm_agent/reference/agentrealm_reference.md), then the facts we measured in [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md), then a short progression arc (survive and learn, gear up, beat levels, beat the world) that it judges the current stage against. That prefix is the same on every call, so it is cached (Anthropic `cache_control`, OpenAI `prompt_cache_key`) and replanning every 15 s stays cheap: cache reads do not count against `AGENTREALM_PLANNER_TOKENS_PER_MIN`, cache writes do, and the default budget fits one full write (PLAN.md A62 has the cost per minute). The trace's strategist `ask` record logs the cached prefix as its size and sha256 digest, not its text; the triggers, state and stack are logged in full. Besides the stack, the planner's State tells it where safe ground is (`safe_ground`, plus `nearest_safe` and `town` with distance and direction) and, once, what its previous reply had rejected and why (`last_reply_rejected`: a dropped op, or a param change that would loosen a survival param; a loosening `set_param` op is dropped when the reply is read). The prefix also says what each survival param means and which way tightens it. When the site changes, refresh the reference and commit it:

```bash
python3 scripts/refresh_planner_reference.py   # fetches agentrealm.gg/docs and /guides, writes the reference
```

The reference is about 72k tokens; core sections always go in and the rest fill about 60k. `AGENTREALM_PLANNER_REFERENCE_SECTIONS=all` sends everything, `core` only the core, and a comma list (`changelog,quick start`) adds those sections to the core.

Copy and edit a file in `python/characters/` to try different behavior (`policy.kind` can be `idle`, `wander`, or `scripted`).

## How the agent thinks

- **Budget.** One API call per character per tick (burst of 3, reads included), and at most one intent per tick. With no decision to make, it sends nothing.
- **World model.** Reads of position, terrain and entities, made only when stale, keep a model of what the character has seen.
- **AI plans, state machine executes.** A planner sets the plan stack; the state machine carries out its top op, tick by tick. No state picks its own goal (PLAN.md [Architecture](PLAN.md#architecture-ai-plans-state-machine-executes)).
- **States** are named behaviors, each with a `guard` (should I run?) and an `act` (what do I send?). **Reflexes** act on what is happening now, whatever the plan says: wake, wait out a death, step off lava, retreat, heal, fight, flee, take a supply in reach, recover the death chest. **Executors** run only for the plan's top op: travel, explore, gather, shop, read or say, break, solve, fight a boss, enter a level, equip, fetch an item, wait.
- **Dispatcher** walks the states in a fixed priority order, reflexes first. Explore comes last and is the **safe default**: with no plan op it explores safe ground, and once that is all explored it pushes on to the nearest frontier outside (still off hazards and away from hostiles), so the agent is never idle. Exactly one state picks where the character moves.
- **Plan** is the goal stack, owned by the **AI planner** (A35, a background thread): it replans on every event (op done or dropped, stuck, death, hurt, new clue, new map, Heal out of food with no regen) and every 15 s, within a per-minute call and token budget. Live directives `goals` override it (manual steering): their ops stay on top of the stack until each is done or dropped, and the planner's goals go below them. With no valid plan the planner layer emits nothing and the safe default runs. The planner speaks only in plan ops (`plan.OP_FIELDS`, exactly the ops the executors run): a new behavior is a new op, never a state that starts itself. With `--no-planner` (a test mode) the built-in planner turns the character file's `policy.goals` into ops. Paths are A* over known tiles, around fog and hazards.
- **Hostiles** are learned too: the API never says which NPC types are monsters. An NPC counts as a threat once its type has swung at us, hit us or died in view this run, or when it is a boss or the one hitting us (`survival.is_hostile`). Flee, Retreat and Fight answer only those, so townsfolk and helpers are left alone; Retreat leaves a fight it would lose only while the group is coming for it (A9, A23).
- **Gem yield** (A63) is learned, not written in: every block the agent cuts is filed with whether a gem came of it, summed per 16×16-block region of the map. The planner sees the best and the barren regions nearby; Gather cuts field cells before safe-zone ones, anywhere off hazards with no hostile near, holds out a cell whose cut had no effect, marks a region or safe zone uncuttable after 3 such cuts (town grass did not cut in run 2), heads out of safe ground when nothing is left to cut, and stops cutting in a region that shows no gem after 30 cuts (barren) unless the planner names it.

Every state and policy key: [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md). Tables in [`PLAN.md`](PLAN.md): [Scheduler](PLAN.md#scheduler-which-call-this-window), [Reflexes](PLAN.md#reflexes-every-round-trip-no-model), [Plan](PLAN.md#plan-goal-and-path).

## Make it yours

This repo is meant for you to fork and extend, not only to run the shipped profiles (`wren`, `kit`) on a character you choose. Start from [`python/starter_agent.py`](python/starter_agent.py): one file that syncs, flees, and explores on its own — copy it and grow your own loop before you touch the full reference agent. It moves one tile at a time with `SetPosition`, waiting out the movement cooldown between moves, where the reference agent sends paced `Step` queues.

```sh
cd python
python3 starter_agent.py create characters/starter.toml --name Starter
python3 starter_agent.py run characters/starter.toml --character-id ID
```

[`docs/MAKE_IT_YOURS.md`](docs/MAKE_IT_YOURS.md) walks through adding a state to the reference agent: guard and act, where it goes in the dispatcher, and how to test it, with a tiny greet example.

## Tests

From the repo root:

```sh
make test
```

`make conductor-test` covers [`tools/conductor`](tools/conductor/README.md) (Node 22+). CI runs both on every pull request.

Live M6 smoke on olympuff: `make smoke-m6-olympuff CHARACTER_ID=…` (or `CHARACTER_NAME=…`, or `AGENTREALM_CHARACTER_ID` exported) with `AGENTREALM_API_KEY` set runs the M6 done-when ([`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md)) via [`scripts/smoke_m6_olympuff.py`](scripts/smoke_m6_olympuff.py). The variables are passed to the script as `--character-id` / `--character-name`; the profile is `python/characters/olympuff_walker.toml`. Lives on live worlds are permanent. The accepted live run (A4) is in [`docs/acceptance/m6_olympuff_PASS.transcript`](docs/acceptance/m6_olympuff_PASS.transcript) (names and ids redacted).

Shared acceptance modules: [`acceptance.py`](python/agentrealm_agent/acceptance.py) (runner hooks, request-error counter), [`acceptance_smoke.py`](python/agentrealm_agent/acceptance_smoke.py) (wake, overworld start, the smoke run loop; M7, M8, M9, M10, M11), [`acceptance_run.py`](python/agentrealm_agent/acceptance_run.py) (`TimedRunHooks`: deaths, API errors, the wall-clock stop; `FULL_RUN_FRACTION`; M7, M8, M9, M10, M11) and [`acceptance_survival.py`](python/agentrealm_agent/acceptance_survival.py) (`SurvivalAcceptanceMetrics`: retreat-miss, Recover-withdraw and loop checks, per-gate survival states; `OscillationAbortTracker`; M7, M9, M11).

Live M7 smoke: `make smoke-m7-olympuff CHARACTER_ID=…` (or `CHARACTER_NAME=…`, or `AGENTREALM_CHARACTER_ID` exported) with `AGENTREALM_API_KEY` set plays one hour on the olympuff overworld via [`scripts/smoke_m7_olympuff.py`](scripts/smoke_m7_olympuff.py), using profile `python/characters/olympuff_m7.toml` and a character you choose at run time. Start with that character on the overworld (a sleeping character is woken with one `Wait` first, and a downed one is waited out): the script sends the agent to one target 150 blocks east of where it stands (`--target X,Y` overrides), then the planner plays.

With the planner on, the target is a directives goal (`travel:point:<map>:<x>:<y>`, set in memory by `acceptance_smoke.pin_goto`, never written to your directives file): it sits on top of the stack, the planner cannot drop it and keeps planning below it, and once the agent stands there (or stuck detection gives it up) it is unpinned for good and the planner owns the stack. A given-up target leaves the stack at once, and any `travel` op (whatever its `to`) that resolves to a cell stuck detection gave up on is dropped, or filtered out of a planner reply, for the rest of the run. The planner's State lists the stack with each op marked `pinned` (directives) or `planner`, so it plans around the target rather than trying to drop it. With `--no-planner` it is the built-in planner's `goto`, in front of the profile's goals. Either way it is a `travel` op, so only **Travel** moves the character until it arrives; reflexes such as Heal and Flee still act. Stuck detection escalates the walk and gives it up like any other.

Every walk commits to the path it chose and keeps it until it arrives, a step is rejected, its target changes, or newly seen terrain blocks it or makes another path more than 20% cheaper; it never turns round for a route that only looks a step cheaper, so two equal routes never alternate (`navigation/walk.py`, A15). The one exception is hostiles: a path that turns back is taken when it is cheaper overall and the kept path's hostile cost is more than 20% higher. When a stuck walk's break opens the way, or an alternate route turns up, the walk starts again from the bottom of the escalation ladder only if it then gets at least 5 cells nearer the target than it ever had (`RESET_PROGRESS_CELLS`); otherwise escalation carries on and the target is eventually given up. The oscillation guard is a safety net that should no longer fire (A61). If the character paces between two cells (6 cell changes over at most 2 cells), the oscillation guard in dispatch gives up the target it was walking to, backs it off like any stuck give-up (reason `pacing`), and writes an `oscillation` event to the trace; Retreat, Fight and Flee pacing with steps of their own back nothing off, while a Heal or Loot walk that paces has its food, pickup or safe tile given up; dispatch then picks something else (`navigation/oscillation.py`). Heal and Loot walks are also given up on no route or no progress in 20 moves or 30 s, so Heal cannot pace beside food it cannot reach. The smoke script aborts with exit 1 and an `ABORT: sustained oscillation` message when the guard gives up a target more than 3 times in 6000 ticks, so a live run never spends its hour pacing. Guard events that gave nothing up (survival states doing the moving) are reported, never an abort.

Pass criteria (PLAN.md A16, checked in [`m7_acceptance.py`](python/agentrealm_agent/m7_acceptance.py)):

- no death (the first one ends the run), and alive at the end of the hour;
- no tick where `should_retreat` held, on the world the decision saw, while a non-survival state ran;
- Recover withdraws only while standing on a known safe tile (the cell the queue puts it on when the `WithdrawFromChest` runs);
- no loop: 24 Step-sending decisions in a row at one cell with one reason (waiting, such as Heal resting, is not a loop);
- no sustained oscillation: more than 3 `oscillation` events that gave up a target within 6000 ticks stops the run at once (events where survival states did the moving and nothing was given up do not count; a Heal or Loot walk given up by the guard counts);
- no API error;
- no return to a given-up target: acting again on a `travel` op that resolves to a cell stuck detection gave up on fails the run (`given_up_returns`, printed in the summary);
- with the planner on, no 3 planner errors in a row (a failed call, a reply that is not a JSON object, or one whose ops are all invalid; a lone error the backoff recovers from is reported, not fatal), no refused key (401/403), and at least one plan accepted; the summary prints both totals (every smoke script does);
- on a run of at least 95% of an hour, safe-zone regen measured (yes or no), and the navigation target reached or given up on.

Give-up rule: navigation passes on a give-up only when stuck detection gave up on that target itself, with a reason (for example, a route that needs a block broken). Give-ups on other goals, such as frontier cells while exploring, are counted in the summary but never pass. Heal actions are reported, not gated.

No live hour has passed yet (A58); runs that did not are in [`docs/observations/A16_live_play.md`](docs/observations/A16_live_play.md) and [`docs/observations/A58_live_play.md`](docs/observations/A58_live_play.md). CI covers the gate, the runner hooks and the navigation fixtures in `python/tests/test_m7_acceptance.py` without live keys.

Regen probe (A60): when the world knowledge base has no regen answer yet, run `make probe-regen CHARACTER_ID=…` (same selection as the M7 smoke) before the hour. [`scripts/probe_regen.py`](scripts/probe_regen.py) plays the normal agent on `python/characters/regen_probe.toml`, which fights a hostile in reach to get hurt, retreats at `should_retreat`, and lets Heal measure safe-zone regen on a known safe tile. It stops when regen answers (a "yes" is saved for the M7 gate), after 30 minutes (`--seconds`), or on the first death, and exits 0 only when regen answered.

Live M8 smoke: `make smoke-m8-olympuff CHARACTER_ID=…` (or `CHARACTER_NAME=…`, or `AGENTREALM_CHARACTER_ID` exported) with `AGENTREALM_API_KEY` set plays one hour on the olympuff overworld via [`scripts/smoke_m8_olympuff.py`](scripts/smoke_m8_olympuff.py), using profile `python/characters/olympuff_m8.toml` and a character you choose at run time. Start on the overworld (a sleeping character is woken with one `Wait` first, and a downed one is waited out).

Pass criteria (PLAN.md A25, checked in [`m8_acceptance.py`](python/agentrealm_agent/m8_acceptance.py)):

- no death (the first one ends the run), and alive at the end of the run;
- no fight started below the health floor (`would_lose` at the first attack after entering **Fight**);
- no API error;
- on a run of at least 95% of an hour: gems earned at least once; armor worn; a shop weapon armed; potion reserve reached; **Heal** took ground food and drank a carried potion; at least one lone weak hostile kill (`NPCDied` for the NPC **Fight** attacked while it was the lone hostile, of a measured type hitting at most 2).

Since A61 nothing equips gear or restocks potions on its own: Equip runs only for a plan `equip` op and Shop only for a `buy` op, so M8 is blocked until the planner (#108, step 2) emits `equip` and potion `buy` ops. No live M8 pass has run yet; the gate and `python/tests/test_m8_acceptance.py` cover the metrics and smoke script without live keys.

Live M9 smoke: `make smoke-m9-olympuff CHARACTER_ID=…` (or `CHARACTER_NAME=…`, or `AGENTREALM_CHARACTER_ID` exported) with `AGENTREALM_API_KEY` set runs the M9 done-when via [`scripts/smoke_m9_olympuff.py`](scripts/smoke_m9_olympuff.py), using profile `python/characters/olympuff_m9.toml`. Start on the overworld. Investigate (A30) walks next to each minimap entrance mark and records the look; the profile's `doors` goal walks door tiles in view, not minimap marks, and `explore` fills the map. Once every required mark is recorded, the script adds `travel:town` to the profile's directives file (`python/characters/olympuff_m9.directives.toml`, re-read on change, A8), so Travel (A27) walks the agent to the knowledge-base town cell; the file's original contents (or its absence) are restored when the run ends. The run ends when every required mark is recorded and the character stands on town, or when the time budget is reached.

Pass criteria (PLAN.md A29, checked in [`m9_acceptance.py`](python/agentrealm_agent/m9_acceptance.py)):

- no death, and alive at the end;
- no tick where `should_retreat` held while a non-survival state ran;
- Recover withdraws only on a known safe tile;
- no loop (24 Steps in a row at one cell with one reason);
- no API error;
- on a run of at least 95% of the default time budget (two hours): every entrance mark within strength looked during this run with its `block_type` (and `needs: key` when locked); looks from a persisted knowledge base do not count: the script clears them and snapshots what is still recorded at run start, before the runner starts, and only marks recorded after the snapshot count (since A61 no state walks to look at a mark on its own, so this clause needs a planner that sends the character there; step 2 of the redirect), and the character on the town cell at the last decision (passing over town earlier does not count).

Marks skipped because the strength bracket closed the cell under `over_strength_ceiling` are counted in the summary, not required; when the bracket closed every mark, nothing is required ("no entrance catalog" means the minimap listed none). Heal actions are reported, not gated. No live M9 pass yet; CI covers the gate in `python/tests/test_m9_acceptance.py` without live keys.

Live M10 smoke: `make smoke-m10-olympuff CHARACTER_ID=…` (or `CHARACTER_NAME=…`, or `AGENTREALM_CHARACTER_ID` exported) with `AGENTREALM_API_KEY` set explores for one hour on olympuff via [`scripts/smoke_m10_olympuff.py`](scripts/smoke_m10_olympuff.py), using profile `python/characters/olympuff_m10.toml` and a character you choose at run time. A sleeping character is woken with one `Wait` first.

Pass criteria (PLAN.md A33, checked in [`m10_acceptance.py`](python/agentrealm_agent/m10_acceptance.py)):

- no death, and alive at the end of the run;
- no Break attempt on a (block, capability) pair already failed in the knowledge base (re-breaking a regrown block a pair opened is fine);
- no API error;
- on a run of at least 95% of the default hour, every readable sign that came into sight along the route has been read, and every NPC that came within 25 blocks has been spoken to;

Break memory sees a `Use` only through `Memory.break_pending`, which only Break sets. Gather's `cut bush` and Solve's `use_block` never set it, so their uses are neither recorded in `kb.breaks` nor counted by the duplicate-break gate.

Since A61 states read signs and speak to NPCs only for a plan `read` or `say` op, so this gate needs a planner that emits them (step 2 of the redirect). No live M10 pass yet (A33 partial); CI covers the gate and the smoke script in `python/tests/test_m10_acceptance.py` without live keys.

M4 (strategist) acceptance is offline: `make test` runs [`python/tests/test_m4_acceptance.py`](python/tests/test_m4_acceptance.py), where the real runner and states play an invented test world against a fake server, with a fake model in place of the LLM. The agent reads a sign, the clue reaches the strategist, and its answer (buy a torch, travel to a hedge, burn it, travel to the entrance behind it) has to be carried out.

Pass criteria (PLAN.md A36, checked in [`m4_acceptance.py`](python/agentrealm_agent/m4_acceptance.py)):

- at least one clue trigger reaches the strategist;
- no API error;
- each required op is planned by an applied strategist answer, run by the state that owns it (`Shop` for `buy`, `Break` for `break_block`, `Travel` for `travel`) with a new queue that acts on it, and finished (popped as done).

Live M11 smoke: `make smoke-m11-olympuff CHARACTER_ID=…` (or `CHARACTER_NAME=…`, or `AGENTREALM_CHARACTER_ID` exported) with `AGENTREALM_API_KEY` set runs the M11 done-when via [`scripts/smoke_m11_olympuff.py`](scripts/smoke_m11_olympuff.py), using profile `python/characters/olympuff_m11.toml` and a character you choose at run time. It needs the AI planner (a key, see **The AI planner**), or the script exits 2, since only the planner pushes the `fight_boss` op that clears a level. Start on the overworld (same wake and respawn wait as M7). The runner plays until it clears a level and attempts the next one, or until the default two-hour wall clock (`--seconds` overrides).

Pass criteria (PLAN.md A40, checked in [`m11_acceptance.py`](python/agentrealm_agent/m11_acceptance.py)):

- no death (the first one ends the run), and alive at the end;
- no tick where `should_retreat` held outside a survival state (including Boss during a boss fight);
- Recover withdraws only on a known safe tile;
- no loop or sustained oscillation (same thresholds as M7);
- no API error;
- on a run of at least 95% of the default limit: at least one `level_clear_ceremony`, then a follow-on attempt: back on the overworld map, then into the interior of a level not yet cleared, or `enter_level` / `fight_boss` on top of the plan while on the overworld map. The gate reads the plan's top op from `Plan.current()` (passed to `before_tick` as `plan_op`), and the op that was on top when the clear arrived never counts, since that fight produced the clear; nor does any `enter_level` / `fight_boss` at that op's door. Only the overworld map counts as having left the level.

A shorter run (`--seconds` under 95% of 7200) judges survival only and prints `PASS (survival only; milestone not judged)`.

No live Olympuff pass yet (A40 partial); CI covers the gate and smoke wiring in `python/tests/test_m11_acceptance.py` without live keys.
