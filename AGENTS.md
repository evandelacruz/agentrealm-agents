# AGENTS.md

Rules for agents working in this repo. Read this first.

## What this is

Reference agents that play Agent Realm through its public API. Read [README.md](README.md), then [PLAN.md](PLAN.md).

The goal is for people to build their own agents for Agent Realm (agentrealm.gg), and for that to be easy and fun. Our agent is the starting point they copy, edit and grow, so it has to be easy to read and easy to change, not only correct.

## Where truth lives

| File | Role |
|---|---|
| [PLAN.md](PLAN.md) | Design, what the API allows today, server gaps, and the backlog: PR-sized items (A1, A2, …) with their dependencies, grouped under milestone headings (M0–M13). |
| [docs/PLAYABLE_AGENT_PLAN.md](docs/PLAYABLE_AGENT_PLAN.md) | Scope and done-when of the milestones M0, M4 and M6–M12. PLAN.md owns the item IDs and dependencies. |
| [docs/GAME_NOTES.md](docs/GAME_NOTES.md) | Game facts the plan relies on, each with its source. |
| [status.json](status.json) | Per-item work state. Not a source of truth. |
| The site's [docs](https://agentrealm.gg/docs) and [guides](https://agentrealm.gg/guides) | The API the agents play against. |
| `main` + open PRs | What shipped, what is in flight. |

There is no ticketing system. PLAN.md is the spec and the backlog.

**Read PLAN.md before asking for a decision.** The expensive failure is re-litigating something already settled.

`status.json` records what we currently think needs doing. Where it disagrees with PLAN.md, status is what is wrong.

## Boundaries

Decided. Do not cross without flagging prominently.

- **An ordinary API client.** HTTP only. Imports nothing from the server and never touches its databases. Something the API does not give is a server gap: write it into PLAN.md **Server gaps**, never build a side door.
- **Python 3.11+, standard library only.** The AI planner (M4, A35) is the one place a dependency may enter (the `anthropic` SDK, listed in `python/requirements.txt` and installed by `make setup`). It is on in every live run, but imported only when it runs, so `--no-planner` and `make test` stay standard library only. The one exception is [`tools/conductor`](tools/conductor/README.md): ops tooling for building this repo, not part of an agent. It is Node 22+ and TypeScript on `@cursor/sdk`, and needs `CURSOR_API_KEY` for every command but `prs`.
- **Obeys the invariants a client can see.** At most one intent per tick, no standing orders, and when there is no decision it sends nothing.
- **Stays inside the call budget.** One request per character per tick, burst of 3, reads included (PLAN.md **What the API gives today**).

## Rules for agents

- Cite backlog IDs (A1, A50, …) in commits and PR bodies.
- Read the cited PLAN.md sections and this file before writing code.
- Write for the newcomer who will copy this agent: plain names, a short docstring on each state, one obvious place to add a behavior. Reviews flag anything that makes the agent harder to read or extend, the same as a bug.
- **Rules, not run patches (A87).** A fix found in a live run either generalizes a rule a state already has, or hands the situational call to the planner. It never adds a branch for the situation the run hit. Code comments state the rule in game terms; the run that found it is cited in the PLAN.md row and `docs/observations`, not in code. `make test` counts run citations (`run N` or `runs N`, any case) in agent source and the starter (`tests/test_architecture.py`) and fails a PR that adds one.
- The agent is character-agnostic: it plays whatever character it is handed at run time (A59). No live character name or id anywhere (code, configs, scripts, README, docs, status.json notes, tests; fixtures use obviously fake names), and characters are created only by an explicit `create` command.
- A live run needs exclusive use of its character. Another client's `tick` replaces the queue, so two clients on one character corrupt both runs' traces (A23 run 2). Do not start a run on a character that another session or agent is playing.
- Fight or flee is one decision per engagement (A94): `Memory.engagement`, synced once per decision in dispatch (`engagement.sync_engagement`). Every state and gate that asks whether to fight reads it through `engagement.fights`, or `engagement.would_fight` for a hostile not yet in range. `survival.would_lose` is retired; never add a second fight test beside the engagement's.
- Which weapon to arm is one rule (A99): `equip.weapon_to_arm`, by the job at hand (fight or cut), ordered by `equip.weapon_rank` (within the highest hunting-ground ceiling known first, then damage). Equip, Gather and the planner's `upgrades_for_sale` read it; never add a second weapon ranking. The one exception today is Break, which picks the tool for a block's capability with `break_memory.pick_supply_for_capability` (not used up first, then armed, then code).
- Keep PLAN.md and README.md matching the code. A behavior change that leaves them describing the old one is not done.
- Run `make test` before pushing, and `make conductor-test` if you touched `tools/conductor`. The `test` GitHub Actions workflow runs both on every PR.
- Tests never read or write `python/.state` (A86): `python/tests/__init__.py` points `AGENTREALM_STATE_DIR` at a temp dir and fails the suite on any file operation there. `knowledge_base.WORLDS_DIR` and `supplies.CACHE_PATH` are bound at import, so a test that patches `config.STATE_DIR` must patch them too.
- Open PRs **ready for review, not draft**. If tooling defaults to draft, run `gh pr ready`.
- **Never merge.** Evan merges, and so does the supervisor ([`.claude/skills/agentrealm-agents-supervisor/SKILL.md`](.claude/skills/agentrealm-agents-supervisor/SKILL.md)). No other agent does.
- **Review verdict:** every pull request has two reviewers, set in [`.github/reviewers`](.github/reviewers): the Opus bot always, plus Cursor or the Sonnet bot. Only trusted reviewers count; anyone else's review is ignored. It is approved when both approved its current head, no trusted reviewer (a person included) rejected it, and no review is running. Open threads never block. Rule: [`agentrealm-agents-fixer`](.claude/skills/agentrealm-agents-fixer/SKILL.md) **The review verdict**. Only Evan edits `.github/reviewers`.
- Do not add dependencies, or change moderation or the call budget or pacing, without flagging prominently.
- If blocked by an open architecture, legal, or moderation question, an open design question, or a server gap, **halt and say why**. Do not invent. Check PLAN.md and the published docs first.
- Backlog items are PR-sized. If one still needs a second PR, ship a reviewable slice, mark the ID `partial` with a `remaining` note in `status.json`, and let the next pass continue it.
- In `status.json`, edit only the entries of the IDs your PR covers, and keep the blank line between entries. Two PRs on different IDs then never touch adjacent lines, so they do not conflict. The exception is a new ID appended after the last entry: it adds a comma to that entry's line, so it can conflict with a PR that edits that entry. Merge `main` and keep both.

## Writer lock

`conductor:working` is the writer lock for **implementers and fixers** (Cursor or Claude Code). Reviewers never take it. `polish-done` marks an approved pull request that has had its one polish pass from a Claude fixer ([`agentrealm-agents-fixer`](.claude/skills/agentrealm-agents-fixer/SKILL.md) **Polish**); the supervisor merges only pull requests that carry it. Only Claude fixers set it.

- Claim it when you start writing. Release it when you finish (after the push, PR ready) or if the spawn that claimed it failed before the agent started. A polish pass releases it and adds `polish-done` in the same label update, even with nothing to push. One exception: a fixer that declines every finding and has nothing to push keeps it and hands the PR to Evan ([`agentrealm-agents-fixer`](.claude/skills/agentrealm-agents-fixer/SKILL.md) **Declined everything**).
- The claim is check-then-add, not atomic. Two writers that start at the same moment can both claim it.
- Skip a PR that already has the label. Do not add a second writer. Do not delete a lock you did not claim in this session.
- Cursor `status` listing an implementer as `finished` means that worker should already have released. If the label is still on the PR, another worker may hold it (Claude Code fixers use the same label and do not appear in that list) or the holder crashed. Leave the label and tell Evan. Do not treat "Cursor finished" as permission to clear it.

## Roles

| Role | Runs in | Skill |
|---|---|---|
| Supervisor: merges what is ready, dispatches fixers or new work (Cursor first, Claude while Cursor is out of credits) | Claude Code | [`.claude/skills/agentrealm-agents-supervisor`](.claude/skills/agentrealm-agents-supervisor/SKILL.md) |
| Fixer fleet: one Claude session per blocked PR, and one per approved PR due its polish pass | Claude Code | [`.claude/skills/agentrealm-agents-fixer-fleet`](.claude/skills/agentrealm-agents-fixer-fleet/SKILL.md) |
| Fixer: one pass on one blocked PR, or the one polish pass on an approved PR | Claude Code | [`.claude/skills/agentrealm-agents-fixer`](.claude/skills/agentrealm-agents-fixer/SKILL.md) |
| Implementer fleet: one Claude session per ready backlog slice, while Cursor is out of credits | Claude Code | [`.claude/skills/agentrealm-agents-implementer-fleet`](.claude/skills/agentrealm-agents-implementer-fleet/SKILL.md) |
| Reviewers: the Opus Review Agent on every ready PR head, and the Sonnet bot when [`.github/reviewers`](.github/reviewers) says `second: sonnet` | GitHub Actions | [`.github/workflows/claude-review.yml`](.github/workflows/claude-review.yml) |
| Conductor: plans, spawns implementers, watches PRs | Cursor | [`.cursor/skills/agentrealm-agents-conductor`](.cursor/skills/agentrealm-agents-conductor/SKILL.md) |
| Fleet: batch of `n` Cursor implementers, the default for new work | Cursor | [`.cursor/skills/agentrealm-agents-fleet`](.cursor/skills/agentrealm-agents-fleet/SKILL.md) |

Cursor agents are spawned with [`tools/conductor`](tools/conductor/README.md).
