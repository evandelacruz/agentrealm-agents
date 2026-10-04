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
python3 -m agentrealm_agent metrics characters/wren.toml
```

Against the public API, skip the seed script: set `AGENTREALM_BASE_URL=https://api.agentrealm.gg` and an API key from your account page.

**The default base URL is `http://localhost:8080`, a local stack.** To play the public API, set `AGENTREALM_BASE_URL=https://api.agentrealm.gg` and use a key from your account page on agentrealm.gg. Lives there are permanent: a character at zero lives is ended. The sample characters set `world = "sandbox"`, the free practice world with the same rules on a different map; set `world` to a live world's code to play there.

`create` saves each character’s id to `python/.state/<name>.json`. `run` drives every listed character until Ctrl-C, one line per window to stdout and a JSONL trace per character in `python/.state/`. `metrics` summarizes the last run in that trace (deaths, kills, gems, levels cleared, time per level; [`PLAN.md`](PLAN.md) A41); the trace keeps every run, and each starts at its `world` record. Characters in the same world share one learned knowledge file at `python/.state/worlds/<world_code>.json` (gitignored): `run` loads it once at start, refusing to start if it is unreadable, and saves it once at exit. Its `items` section holds, per `supply_subtype_code`, the weapon reach learned from a `target_out_of_range` rejection and the last `gem_price` seen ([`PLAN.md`](PLAN.md) A18). Run one `run` process per world at a time; two would each save their own copy, and the last to exit wins. Stopping the agent leaves the characters in the world where they stand. The game runs at 10 ticks per second; [`PLAN.md`](PLAN.md) Real time says how the agent keeps up.

## Make a character

Copy a file from `python/characters/` and edit it. Names must be unique in the world.

| Key | Meaning |
|---|---|
| `name`, `avatar`, `model_agent`, `world` | Identity. `avatar` is an outfit code. `model_agent` is what rankings aggregate on. |
| `policy.kind` | `idle` sends nothing. `wander` takes random steps. `scripted` runs the reflexes and goals below through the **Explore** state (A5). Before each `POST tick`, a priority dispatcher picks **Sync**, **Downed**, **Escape**, **Retreat**, **Heal** when hurt and no hostile is in range (A10), **Flee**, **Recover**, **Loot**, **Explore**, or **Idle**, and that state returns the intents to send. **Escape** steps off `avoid_blocks` ground. **Retreat** walks to the nearest known safe tile when the next `retreat_hits` hits from the hostiles in range (sized by the threat table) could kill; it needs a hostile in range and a known safe tile, and never runs with `on_hostile = "ignore"`. **Flee** carries out `policy.on_hostile` below, breaking ties toward a known safe tile, and stays put on one (A9). |
| `policy.goals` | Tried in order: `explore`, `doors`, `goto` (with `policy.goto = [x, y]`, optional `policy.goto_map = <map_id>` for another map), `wander`, `hold`. Paths may run through unseen ground (fog costs 2 per step to known ground's 1), but the agent only steps onto tiles it has seen; a goal whose next step is unseen yields to the next goal. A search that runs out of its node budget (`goto` on its own map, the death chest) plans only the steps inside perception, along a coarse corridor over 16×16 map tiles that is searched a little each replan and kept until the goal or map changes or a step is rejected. With a shared knowledge file, door warps and terrain are remembered across runs; `goto_map` routes through known doors, then A* on each map. The `doors` goal prefers doors whose destination is not yet recorded. A rejected step teaches the map by code (a `not_traversable` cell stays blocked until a `BlockChanged` on it, a `block_occupied` cell is skipped one move then priced high for 30 ticks, a locked door is marked `locked` on its door entry in the knowledge file; PLAN.md reflex 1 has the full table). |
| `policy.on_hostile` | `flee`, `fight`, or `ignore`, for anything in `policy.hostile` (`npc`, `character`) within `policy.hostile_range`. `fight` swings only at a character (NPCs until A23) not in `never_attack`, and flees anything else. |
| `policy.pickup` | **Loot** (A20): walks to free supplies in sight and takes them (`Take`), and withdraws the best supply from a ground chest within reach (`WithdrawFromChest` with that one id). With the carried chest full (10 slots, counting held, worn, armed and stowed), it drops the lowest-valued held supply first, but only for a pickup worth more; otherwise it skips the pickup and lets **Explore** run. Value is the item table's `gem_price`, then potions and food, then anything else. Priced supplies are the shop's and the death chest is **Recover**'s. A `carry_capacity_full` rejection lowers the assumed capacity to what is carried; a `Drop` refused `not_transferable` is never retried. Hearts first is off until a life's supply code is known (GAME_NOTES open questions). |
| `policy.avoid_blocks` | Block types to step off and to plan around. Standing on one with no safe step off, the plan crosses as few as it can. Other fire and lava cost their `occupy_damage` per step. |
| `policy.entity_refresh` | Ticks between entity reads when nothing is happening. |
| `policy.seed` | Random seed for `wander`. Defaults to the character id. |

Each character may also have `python/characters/<name>.directives.toml` beside its character file. The runner re-reads it when the file changes; a file that fails to read or parse keeps the last good directives (defaults if none loaded yet); deleting the file restores the defaults. `never_attack` lists kinds or NPC type codes the agent must not swing at (`character`, or an NPC `code`); a fight reflex that would `Use` a forbidden target flees instead, and any other `Use` aimed at one is replaced by a `Wait`. A `Use` on the character itself (**Heal** eating or drinking) is not an attack and always passes. `params` tune survival: `retreat_hits`, `risk`, and `lives_floor` set when **Retreat** fires from health and the threat table. `fight_margin` feeds only the win estimate that **Fight** (A23) will gate on; nothing acts on it yet. Out-of-range param values are ignored. `goals` and `instructions` are kept for the strategist.

In a calm window it would otherwise skip, the agent calls `get_zone` on one revealed cell: first within 8 blocks of town or its last respawn point, then on the cells of its planned path. It records which are safe for retreat (A9), **Heal** (A10) and death-chest recovery (A11). When hurt and out of combat, **Heal** walks to or takes food in sight, then `Arm` + `Use` self on carried food or a potion (the weapon is not re-armed until A24), rests in a safe zone once health has been seen coming back there, otherwise waits in town and raises a `buy` signal for potions that nothing reads until Shop (A21). With no reachable known safe tile, or after 600 ticks of waiting with no health back, it yields to Explore for 300 ticks.

With `policy.pickup` on, **Recover** walks back to a dropped death chest on this map only once a safe tile on or next to it is known; until then, or while no step toward it can be planned, it keeps exploring. Only that tile is checked, not the route to it. **Escape**, **Retreat** and **Flee** outrank it, and the fight and pickup reflexes still run first. Urgent windows and the `idle` policy never read zones.

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

### M6 acceptance (live Olympuff, A4)

`scripts/smoke_m6_olympuff.py` runs the M6 done-when against the public API; no live PASS is recorded in the repo yet: 200 applied `Step`s, no `movement_cooldown` rejections, and calm windows spend under a quarter of the budget on `POST tick` (the 4–10 tick poll cadence; terrain and zone reads use spare windows). This uses a dedicated character (`python/characters/olympuff_walker.toml`); lives on live worlds are permanent.

```sh
export AGENTREALM_BASE_URL=https://api.agentrealm.gg
export AGENTREALM_API_KEY=...   # from your account page
make smoke-m6-olympuff
# or: python3 scripts/smoke_m6_olympuff.py --timeout 3600
```

The script creates the character on first run (id in `python/.state/OlympuffWalker.json`), then drives `run` until the step count is met. It prints a short summary and exits non-zero on failure. Traces land in `python/.state/OlympuffWalker.trace.jsonl`.
