# AGENTS.md

Rules for agents working in this repo. Read this first.

## What this is

Reference agents that play Agent Realm through its public API. Read [README.md](README.md), then [PLAN.md](PLAN.md).

## Where truth lives

| File | Role |
|---|---|
| [PLAN.md](PLAN.md) | Design, what the API allows today, server gaps, and the backlog: PR-sized items (A1, A2, …) with their dependencies, grouped under milestone headings (M0–M12). |
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
- **Python 3.11+, standard library only.** The LLM planner (M4) is the one place a dependency may enter, and it stays optional. The one exception is [`tools/conductor`](tools/conductor/README.md): ops tooling for building this repo, not part of an agent. It is Node 22+ and TypeScript on `@cursor/sdk`, and needs `CURSOR_API_KEY` for every command but `prs`.
- **Obeys the invariants a client can see.** At most one intent per tick, no standing orders, and when there is no decision it sends nothing.
- **Stays inside the call budget.** One request per character per tick, burst of 3, reads included (PLAN.md **What the API gives today**).

## Rules for agents

- Cite backlog IDs (A1, M5, …) in commits and PR bodies.
- Read the cited PLAN.md sections and this file before writing code.
- Keep PLAN.md and README.md matching the code. A behavior change that leaves them describing the old one is not done.
- Run `make test` before pushing, and `make conductor-test` if you touched `tools/conductor`. The `test` GitHub Actions workflow runs both on every PR.
- Open PRs **ready for review, not draft**. If tooling defaults to draft, run `gh pr ready`.
- **Never merge.** Evan merges, and so does the supervisor ([`.claude/skills/agentrealm-agents-supervisor/SKILL.md`](.claude/skills/agentrealm-agents-supervisor/SKILL.md)). No other agent does.
- Do not add dependencies, or change moderation or the call budget or pacing, without flagging prominently.
- If blocked by an open architecture, legal, or moderation question, an open design question, or a server gap, **halt and say why**. Do not invent. Check PLAN.md and the published docs first.
- Backlog items are PR-sized. If one still needs a second PR, ship a reviewable slice, mark the ID `partial` with a `remaining` note in `status.json`, and let the next pass continue it.
- In `status.json`, edit only the entries of the IDs your PR covers, and keep the blank line between entries. Two PRs on different IDs then never touch adjacent lines, so they do not conflict.

## Writer lock

`conductor:working` is the writer lock for **implementers and fixers** (Cursor or Claude Code). Reviewers never take it.

- Claim it when you start writing. Release it when you finish (after the push, PR ready) or if the spawn that claimed it failed before the agent started.
- The claim is check-then-add, not atomic. Two writers that start at the same moment can both claim it.
- Skip a PR that already has the label. Do not add a second writer. Do not delete a lock you did not claim in this session.
- Cursor `status` listing an implementer as `finished` means that worker should already have released. If the label is still on the PR, another worker may hold it (Claude Code fixers use the same label and do not appear in that list) or the holder crashed. Leave the label and tell Evan. Do not treat "Cursor finished" as permission to clear it.

## Roles

| Role | Runs in | Skill |
|---|---|---|
| Supervisor: merges what is ready, dispatches fixers or new work | Claude Code | [`.claude/skills/agentrealm-agents-supervisor`](.claude/skills/agentrealm-agents-supervisor/SKILL.md) |
| Fixer fleet: one Claude session per blocked PR | Claude Code | [`.claude/skills/agentrealm-agents-fixer-fleet`](.claude/skills/agentrealm-agents-fixer-fleet/SKILL.md) |
| Fixer: one pass on one blocked PR | Claude Code | [`.claude/skills/agentrealm-agents-fixer`](.claude/skills/agentrealm-agents-fixer/SKILL.md) |
| Conductor: plans, spawns implementers, watches PRs | Cursor | [`.cursor/skills/agentrealm-agents-conductor`](.cursor/skills/agentrealm-agents-conductor/SKILL.md) |
| Fleet: batch of `n` Cursor implementers | Cursor | [`.cursor/skills/agentrealm-agents-fleet`](.cursor/skills/agentrealm-agents-fleet/SKILL.md) |

Cursor agents are spawned with [`tools/conductor`](tools/conductor/README.md).
