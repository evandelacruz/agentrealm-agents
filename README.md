# Agent Realm reference agents

Example Python agents that play [Agent Realm](https://agentrealm.gg) through its public HTTP API—no server code, no install step beyond Python 3.11+. Clone this repo, point it at your account, and a character starts moving in the world.

More depth: [`PLAN.md`](PLAN.md) (design and backlog), [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md) (game facts), [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md) (playable-agent milestones), [`AGENTS.md`](AGENTS.md) (rules for agents that build this repo). The site’s [guides](https://agentrealm.gg/guides) and [docs](https://agentrealm.gg/docs) describe the API itself.

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

The default API is `https://api.agentrealm.gg`. Sample characters use `world = "sandbox"` (free practice); set another world code to play live—lives there are permanent. `create` stores the character id under `python/.state/`; `run` plays until Ctrl-C and writes a trace there. See [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md) for metrics, shared world knowledge, and running multiple characters.

Copy and edit a file in `python/characters/` to try different behavior (`policy.kind` can be `idle`, `wander`, or `scripted`).

## How the agent thinks

Each tick the runner spends one API call: read position, terrain, or entities when stale, otherwise `POST tick` with an intent (or nothing). That keeps inside the game’s one-request-per-tick budget.

**Reflexes** are cheap rules that run every tick before anything else—step off lava, take a supply underfoot, flee a nearby hostile, or walk the next step of the current path. They need no model.

**States** are named behaviors with a `guard` (should I run?) and `act` (what intent do I send?). A **dispatcher** walks a fixed priority list each tick—survival first (sync, downed, escape, retreat, heal, fight, flee), then loot and recovery, investigate and puzzle ops, gather and travel, boss and level maps, then explore and idle. If a higher state has nothing to send this window, dispatch **falls through** so the agent never stalls.

**Plan** is the path the agent is walking: goals from the character file (or live directives) plus A* over known map tiles, with fog, hazards, and remembered doors. Explore follows that path until something more urgent wins the round.

Together: reads refresh a **world model**, the plan picks a direction, reflexes and states turn that into at most one intent queue per call. Full policy keys, every state, and directives are in [`docs/CHARACTER_AND_STATES.md`](docs/CHARACTER_AND_STATES.md); scheduler and reflex tables are in [`PLAN.md`](PLAN.md).

## Make it yours

This repo is meant for you to fork and extend—not only to run Wren and Kit as shipped.

- **[`docs/MAKE_IT_YOURS.md`](docs/MAKE_IT_YOURS.md)** (backlog A51): step-by-step guide to adding your own state, wiring it into the dispatcher, testing it, and tuning behavior with directives.
- **Starter agent** (backlog A52): a single small file you can copy and grow (sync, walk, flee, explore)—coming after the guide lands.

Until A52 ships, start from `python/characters/` and the modules under `python/agentrealm_agent/states/`.

## Tests

From the repo root:

```sh
make test
```

`make conductor-test` covers [`tools/conductor`](tools/conductor/README.md) (Node 22+). CI runs both on every pull request.

Live M6 smoke against Olympuff: `make smoke-m6-olympuff` with `AGENTREALM_API_KEY` set (see [`scripts/smoke_m6_olympuff.py`](scripts/smoke_m6_olympuff.py) and [`docs/acceptance/m6_olympuff_PASS.transcript`](docs/acceptance/m6_olympuff_PASS.transcript)).
