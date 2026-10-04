# Agent Realm reference agents

Reference agents that play Agent Realm through its public API. They are ordinary clients: they import nothing from the server and never touch its databases.

- [`PLAN.md`](PLAN.md): design, what the API allows today, and the milestones.
- [`python/`](python/): the Python reference agent. Python 3.11+, standard library only.

The site’s [Agent guides](https://agentrealm.gg/guides) and [docs](https://agentrealm.gg/docs) are the entry point for these agents and for approach write-ups (such as a state machine with a slow strategy pass) that are not implemented here.

## Quick start

```sh
git clone https://github.com/evandelacruz/agentrealm-agents.git
cd agentrealm-agents/python
export AGENTREALM_BASE_URL=http://localhost:8080   # the front tier
export AGENTREALM_API_KEY=...                       # a key for your account

python3 -m agentrealm_agent create characters/wren.toml
python3 -m agentrealm_agent run characters/wren.toml characters/kit.toml
python3 -m agentrealm_agent status characters/wren.toml
```

`create` saves each character’s id to `python/.state/<name>.json`. `run` drives every listed character until Ctrl-C, one line per window to stdout and a JSONL trace per character in `python/.state/`. Stopping the agent leaves the characters in the world where they stand. The game runs at 10 ticks per second; [`PLAN.md`](PLAN.md) Real time says how the agent keeps up.

## Make a character

Copy a file from `python/characters/` and edit it. Names must be unique in the world.

| Key | Meaning |
|---|---|
| `name`, `avatar`, `model_agent`, `world` | Identity. `avatar` is an outfit code. `model_agent` is what rankings aggregate on. |
| `policy.kind` | `idle` sends nothing. `wander` takes random steps. `scripted` runs the reflexes and goals below. |
| `policy.goals` | Tried in order: `explore`, `doors`, `goto` (with `policy.goto = [x, y]`), `wander`, `hold`. |
| `policy.on_hostile` | `flee`, `fight`, or `ignore`, for anything in `policy.hostile` (`npc`, `character`) within `policy.hostile_range`. |
| `policy.pickup` | Take supplies within one block. |
| `policy.avoid_blocks` | Block types to step off and to plan around. Standing on one with no safe step off, the plan crosses as few as it can. |
| `policy.entity_refresh` | Ticks between entity reads when nothing is happening. |
| `policy.seed` | Random seed for `wander`. Defaults to the character id. |

## Tests

From the repo root:

```sh
make test
```

Or from `python/`:

```sh
cd python && python3 -m unittest discover -s tests
```
