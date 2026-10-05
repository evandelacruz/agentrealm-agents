# Agent Realm reference agents

Example agents that play [Agent Realm](https://agentrealm.gg) through its public HTTP API. Clone this repo, point it at your account, and a character starts moving in the world. They are ordinary clients: they import nothing from the server and never touch its databases. The agent lives in [`python/`](python/): Python 3.11+, standard library only, nothing to install or host.

More depth: [`PLAN.md`](PLAN.md) (design and backlog), [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md) (character files, every state, directives, metrics), [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md) (game facts), [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md) (playable-agent milestones), [`AGENTS.md`](AGENTS.md) (rules for agents that build this repo). The site’s [guides](https://agentrealm.gg/guides) and [docs](https://agentrealm.gg/docs) describe the API itself.

## Quick start

You need an [agentrealm.gg](https://agentrealm.gg) account and an API key from your account page.

```sh
git clone https://github.com/evandelacruz/agentrealm-agents.git
cd agentrealm-agents
export AGENTREALM_API_KEY=...   # from your account page

cd python
python3 -m agentrealm_agent create characters/wren.toml
python3 -m agentrealm_agent run characters/wren.toml
```

Other commands:

```sh
python3 -m agentrealm_agent run characters/wren.toml characters/kit.toml   # several characters at once
python3 -m agentrealm_agent status characters/wren.toml                     # where the character is now
python3 -m agentrealm_agent metrics characters/wren.toml                    # summary of the last run
python3 -m agentrealm_agent compare-metrics baseline.metrics candidate.metrics   # candidate-minus-baseline deltas
```

The default API is `https://api.agentrealm.gg` (`AGENTREALM_BASE_URL` overrides it). Sample characters use `world = "sandbox"` (free practice); set another world code to play live, where lives are permanent. `create` stores the character id under `python/.state/`; `run` plays until Ctrl-C and writes a trace there. Run one `run` process per world at a time. Optional strategist (A35): set `AGENTREALM_STRATEGIST_MODEL` and `AGENTREALM_STRATEGIST_API_KEY` or `OPENAI_API_KEY`; limits and triggers are in [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md) **Strategist**. More in [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md) and [`PLAN.md`](PLAN.md) **CLI**.

Copy and edit a file in `python/characters/` to try different behavior (`policy.kind` can be `idle`, `wander`, or `scripted`).

## How the agent thinks

- **Budget.** One API call per character per tick (burst of 3, reads included), and at most one intent per tick. With no decision to make, it sends nothing.
- **World model.** Reads of position, terrain and entities, made only when stale, keep a model of what the character has seen.
- **Reflexes** run first every tick and need no model: step off lava, take a supply underfoot, flee a close hostile, walk the next step of the path.
- **States** are named behaviors, each with a `guard` (should I run?) and an `act` (what do I send?).
- **Dispatcher** walks the states in a fixed priority order: survival first, then loot and recovery, then puzzles, travel and bosses, then explore and idle. A state with nothing to send falls through to the next, so the agent never stalls.
- **Plan** is the path being walked: goals from the character file or live directives, A* over known tiles, around fog and hazards. An optional **strategist** (background thread) can replace the goal stack from an LLM when `AGENTREALM_STRATEGIST_MODEL` and an API key are set; without it, clue text still steers exploration (A32).

Every state and policy key: [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md). Tables in [`PLAN.md`](PLAN.md): [Scheduler](PLAN.md#scheduler-which-call-this-window), [Reflexes](PLAN.md#reflexes-every-round-trip-no-model), [Plan](PLAN.md#plan-goal-and-path).

## Make it yours

This repo is meant for you to fork and extend, not only to run Wren and Kit as shipped. Start from [`python/starter_agent.py`](python/starter_agent.py): one file that syncs, flees, and explores on its own — copy it and grow your own loop before you touch the full reference agent. It moves one tile at a time with `SetPosition`, waiting out the movement cooldown between moves, where the reference agent sends paced `Step` queues.

```sh
cd python
python3 starter_agent.py create characters/starter.toml
python3 starter_agent.py run characters/starter.toml
```

[`docs/MAKE_IT_YOURS.md`](docs/MAKE_IT_YOURS.md) walks through adding a state to the reference agent: guard and act, where it goes in the dispatcher, and how to test it, with a tiny greet example.

## Tests

From the repo root:

```sh
make test
```

`make conductor-test` covers [`tools/conductor`](tools/conductor/README.md) (Node 22+). CI runs both on every pull request.

Live M6 smoke against Olympuff: `make smoke-m6-olympuff` with `AGENTREALM_API_KEY` set runs the M6 done-when ([`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md)) against the public API via [`scripts/smoke_m6_olympuff.py`](scripts/smoke_m6_olympuff.py) and fails on any API error. It uses a dedicated character, `python/characters/olympuff_walker.toml`; lives on live worlds are permanent. If its `.state` entry is lost, a create refused `identity_reuse` reuses the same-named character. The accepted live run (A4) is in [`docs/acceptance/m6_olympuff_PASS.transcript`](docs/acceptance/m6_olympuff_PASS.transcript).

Live M7 smoke: `make smoke-m7-olympuff` with `AGENTREALM_API_KEY` set plays one hour on the Olympuff overworld via [`scripts/smoke_m7_olympuff.py`](scripts/smoke_m7_olympuff.py), on its own character, `python/characters/olympuff_m7.toml` (`OlympuffSurvivor`). Start it with the character on the overworld (a sleeping character is woken with one `Wait` first, and a downed one is waited out): the script sends the agent to one `goto` target 150 blocks east of where it stands (`--target X,Y` overrides), then explores.

While the `goto` target is still owed, the walk comes first: Loot, Shop, Investigate and Travel do not walk anywhere else, Break opens only a block the goto's own stuck escalation picked, and the strategist plan's moves and `wait` hold are skipped. Heal is the exception: a hurt character still walks to food or a safe tile. Stuck detection keeps running on the goto path, so a give-up still ends the deferral.

Pass criteria (PLAN.md A16, checked in [`m7_acceptance.py`](python/agentrealm_agent/m7_acceptance.py)):

- no death (the first one ends the run), and alive at the end of the hour;
- no tick where `should_retreat` held, on the world the decision saw, while a non-survival state ran;
- Recover withdraws only while standing on a known safe tile (the cell the queue puts it on when the `WithdrawFromChest` runs);
- no loop: 24 Step-sending decisions in a row at one cell with one reason (waiting, such as Heal resting, is not a loop);
- no API error;
- on a run of at least 95% of an hour, safe-zone regen measured (yes or no), and the navigation target reached or given up on.

Give-up rule: navigation passes on a give-up only when stuck detection gave up on that target itself, with a reason (for example, a route that needs a block broken). Give-ups on other goals, such as frontier cells while exploring, are counted in the summary but never pass. Heal actions are reported, not gated.

If the account is at its character cap, the create fails: free a slot, or pass `--character` with a TOML naming a character you already have (its lives carry over, so a worn character can fail the hour). No live hour has passed yet (A58); runs that did not are in [`docs/observations/A16_live_play.md`](docs/observations/A16_live_play.md) and [`docs/observations/A58_live_play.md`](docs/observations/A58_live_play.md). CI covers the gate, the runner hooks and the navigation fixtures in `python/tests/test_m7_acceptance.py` without live keys.
