# `@agentrealm-agents/conductor`

Thin CLI that helps a Cursor agent (or you) play **agentrealm-agents build conductor**:
spawn implementer cloud agents, follow up on review fixes, and summarize open
PRs.

This is ops tooling for building the reference agents. It is not part of an agent.

**Dependency flag:** this package is Node + TypeScript and depends on
`@cursor/sdk`. The agents stay Python, standard library only. There is no
first-party Python SDK for Cursor cloud agents.

## Setup

Requires Node 22+.

```bash
export CURSOR_API_KEY=…   # https://cursor.com/dashboard/api
npm --prefix tools/conductor install
```

`prs` only needs `gh` auth. `spawn`, `follow-up`, and `status` need
`CURSOR_API_KEY`.

Open PR summaries include merge-conflict state (`merge:conflict` / `merge:ok`)
and the writer lock (`lock:working` / `lock:none`). Review verdicts come from
submitted reviews on the **current head** only, never from labels, per the
conductor skill's **Review verdicts**: Cursor's latest `APPROVED` /
`CHANGES_REQUESTED` review, and Claude Code's latest review body (posted as
`COMMENTED` under Evan's account). Either at changes requested is
`CHANGES_REQUESTED`; both approved is `APPROVED`; anything else is
`review:none`. A review on an older head does not count. `conductor:working`
is the only label with meaning.

A review still running is the GitHub check
`Cursor Automation: Saims Ref Agent Auto Code Review` (override with
`CONDUCTOR_REVIEW_CHECK` if the automation is renamed). `follow-up` and
`spawn --pr` refuse to start while that check is running. `prs` prints
`review-check:running`.

"Needs fixer follow-up" lists PRs with a merge conflict, red CI, changes
requested, or unresolved threads without approval.

`spawn --pr` and `follow-up` add `conductor:working` before the agent starts
and refuse a PR that already has `conductor:working`. Implementers and
fixers claim that lock and release it when they finish; reviewers never take
it. Do not delete the label so the command will accept the PR. Claude Code
fixers use the same lock and do not appear in `status`. The prompt tells the
agent that holds the lock to remove only `conductor:working` after the push.
A spawn with no `--pr` tells the agent to add that label once the PR exists.

The lock is released only when the command fails before the agent is sent its
prompt. Once the agent is running, a failure (for example `--wait` losing the
run) leaves the label on, because the agent is still writing. The claim is
check-then-add, not atomic: two writers that start at the same moment can both
claim it.

## Commands

```bash
# Spawn an implementer (defaults to env evandelacruz/agentrealm-agents, auto-PR on)
npm --prefix tools/conductor run spawn -- \
  --ids A8 \
  --name "Runtime directives" \
  -- "Implement A8 per PLAN.md …"

# Follow up on an existing agent. --pr claims conductor:working first.
npm --prefix tools/conductor run follow-up -- --agent bc-… --pr https://github.com/evandelacruz/agentrealm-agents/pull/18 -- "Fix unresolved review comments"

# List recent cloud agents (SDK source). Missing statuses are hydrated under
# the same rate-limit pacing as --running-count, so a large --limit is slow,
# not a failure.
npm --prefix tools/conductor run status

# Count cloud agents still running, all pages. Prints one integer; exits
# non-zero if any agent's status cannot be read. Uses Agent.list's status and
# calls listRuns only when it is missing, 4 at a time and at most one start
# per 250 ms: list_agent_runs is limited to 300 requests/minute, so never fan
# out one call per agent or rely on the concurrency cap alone.
npm --prefix tools/conductor run status -- --running-count

# Open PRs + unresolved comment counts (via gh)
npm --prefix tools/conductor run prs
```

Prompt text may be passed as trailing args or on stdin.

## Defaults

| | |
|---|---|
| Environment | `evandelacruz/agentrealm-agents` (named Cursor cloud env; includes this repo) |
| Model | `composer-2.5` (override with `--model` or `CURSOR_MODEL`); Composer runs with `fast=false` (set `CURSOR_MODEL_FAST=true` for the fast variant) |
| Auto-PR | on (`--no-pr` to disable) |
| PR state | ready for review (not draft), enforced by the conductor skill and implementer brief; mark with `gh pr ready` if a draft appears |
| Starting ref | env’s repo checkout (override with `--ref` only when using `--repo`) |

## Conductor skill

Invoke `/agentrealm-agents-conductor` (or ask for a conductor pass). The skill
lives at `.cursor/skills/agentrealm-agents-conductor/`.

Each pass watches open PRs for blocking review comments and also re-reads
**approved but unmerged** PRs for nits and documentation asks worth doing
before Evan merges. `prs` groups PRs by which of those two paths they need;
the skill holds the triage rules and the "never merge" policy.
