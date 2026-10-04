# Agent Realm reference agents

Reference agents that play Agent Realm through its public API. They are ordinary clients: they import nothing from the server and never touch its databases.

- [`PLAN.md`](PLAN.md): design, what the API allows today, and the backlog (PR-sized items grouped under milestones).
- [`docs/PLAYABLE_AGENT_PLAN.md`](docs/PLAYABLE_AGENT_PLAN.md): the plan to make the agent able to play: state machine, LLM strategist, and the scope of milestones M0, M4 and M6–M12.
- [`docs/GAME_NOTES.md`](docs/GAME_NOTES.md): the game facts that plan relies on, each with its source.
- [`python/`](python/): the Python reference agent. Python 3.11+, standard library only.
- [`AGENTS.md`](AGENTS.md): rules for coding agents, and the supervisor, conductor, and worker roles that build this repo.

The site’s [Agent guides](https://agentrealm.gg/guides) and [docs](https://agentrealm.gg/docs) are the entry point for these agents and for approach write-ups (such as a state machine with a slow strategy pass) that are not implemented here.

## Quick start

With the [game stack](https://agentrealm.gg/docs/manual#13-running-the-stack-locally) running (`make up` in that repo, front tier on port 8080):

```sh
git clone https://github.com/evandelacruz/agentrealm-agents.git
cd agentrealm-agents
export AGENTREALM_STACK_DIR=/path/to/agentrealm   # compose project root, for signup logs
python3 scripts/seed_local_stack.py               # account + API key; re-runs reuse the key
source python/.state/local.env                    # export lines, mode 0600, git-ignored

cd python
python3 -m agentrealm_agent create characters/wren.toml
python3 -m agentrealm_agent run characters/wren.toml characters/kit.toml
python3 -m agentrealm_agent status characters/wren.toml
```

Against the public API, skip the seed script: set `AGENTREALM_BASE_URL=https://api.agentrealm.gg` and an API key from your account page.

**The default base URL is `http://localhost:8080`, a local stack.** To play the public API, set `AGENTREALM_BASE_URL=https://api.agentrealm.gg` and use a key from your account page on agentrealm.gg. Lives there are permanent: a character at zero lives is ended. The sample characters set `world = "sandbox"`, the free practice world with the same rules on a different map; set `world` to a live world's code to play there.

`create` saves each character’s id to `python/.state/<name>.json`. `run` drives every listed character until Ctrl-C, one line per window to stdout and a JSONL trace per character in `python/.state/`. Characters in the same world share one learned knowledge file at `python/.state/worlds/<world_code>.json` (gitignored): `run` loads it once at start, refusing to start if it is unreadable, and saves it once at exit. Its `items` section holds, per `supply_subtype_code`, the weapon reach learned from a `target_out_of_range` rejection and the last `gem_price` seen ([`PLAN.md`](PLAN.md) A18). Run one `run` process per world at a time; two would each save their own copy, and the last to exit wins. Stopping the agent leaves the characters in the world where they stand. The game runs at 10 ticks per second; [`PLAN.md`](PLAN.md) Real time says how the agent keeps up.

## Make a character

Copy a file from `python/characters/` and edit it. Names must be unique in the world.

| Key | Meaning |
|---|---|
| `name`, `avatar`, `model_agent`, `world` | Identity. `avatar` is an outfit code. `model_agent` is what rankings aggregate on. |
| `policy.kind` | `idle` sends nothing. `wander` takes random steps. `scripted` runs the reflexes and goals below through **Gather** and **Explore** (A5). Before each `POST tick`, a priority dispatcher picks **Sync**, **Downed**, **Gather**, **Explore**, or **Idle**, and that state returns the intents to send. |
| `policy.goals` | Tried in order: `explore`, `doors`, `goto` (with `policy.goto = [x, y]`, optional `policy.goto_map = <map_id>` for another map), `wander`, `hold`. Paths may run through unseen ground (fog costs 2 per step to known ground's 1), but the agent only steps onto tiles it has seen; a goal whose next step is unseen yields to the next goal. A search that runs out of its node budget (`goto` on its own map, the death chest) plans only the steps inside perception, along a coarse corridor over 16×16 map tiles that is searched a little each replan and kept until the goal or map changes or a step is rejected. With a shared knowledge file, door warps and terrain are remembered across runs; `goto_map` routes through known doors, then A* on each map. The `doors` goal prefers doors whose destination is not yet recorded. A rejected step teaches the map by code (a `not_traversable` cell stays blocked until a `BlockChanged` on it, a `block_occupied` cell is skipped one move then priced high for 30 ticks, a locked door is marked `locked` on its door entry in the knowledge file; PLAN.md reflex 1 has the full table). |
| `policy.on_hostile` | `flee`, `fight`, or `ignore`, for anything in `policy.hostile` (`npc`, `character`) within `policy.hostile_range`. |
| `policy.pickup` | Take supplies within one block. |
| `policy.avoid_blocks` | Block types to step off and to plan around. Standing on one with no safe step off, the plan crosses as few as it can. Other fire and lava cost their `occupy_damage` per step. |
| `policy.entity_refresh` | Ticks between entity reads when nothing is happening. |
| `policy.seed` | Random seed for `wander`. Defaults to the character id. |

Each character may also have `python/characters/<name>.directives.toml` beside its character file. The runner re-reads it when the file changes; a file that fails to read or parse keeps the last good directives (defaults if none loaded yet); deleting the file restores the defaults. `never_attack` lists kinds or NPC type codes the agent must not swing at (`character`, or an NPC `code`); a fight reflex that would `Use` a forbidden target flees instead, and any other `Use` aimed at one is replaced by a `Wait`. A `goals` entry like `gather_gems:20` runs **Gather** until the gem counter reaches 20: it `Use`s grass and bushes and `Take`s gem piles in safe-ish ground (known safe zones, cells touching one, or within the town respawn probe ring, with no hostile in range). Other goal ops and `params` (for example `fight_margin`, `risk`, `lives_floor`) are parsed and validated, out-of-range values ignored, but not read yet. `instructions` is kept for the strategist.

In a calm window it would otherwise skip, the agent calls `get_zone` on one revealed cell: first within 8 blocks of town or its last respawn point, then on the cells of its planned path. It records which are safe for later retreat and healing (A7); nothing acts on them yet. Urgent windows and the `idle` policy never read zones.

## Tests

From the repo root:

```sh
make test
```

Or from `python/`:

```sh
cd python && python3 -m unittest discover -s tests
```

`make conductor-test` builds and tests [`tools/conductor`](tools/conductor/README.md) (Node 22+). CI runs both on every pull request.
