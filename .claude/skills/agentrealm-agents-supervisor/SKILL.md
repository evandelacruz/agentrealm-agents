---
name: agentrealm-agents-supervisor
description: >
  Stand in for Evan above the agentrealm-agents conductors: merge what is ready, send a
  Claude fixer fleet at idle pull requests, and top up with a Cursor
  implementer fleet when nothing is idle. Use when Evan asks to supervise,
  run the supervisor, take over merging and dispatch, or loop it
  (/loop agentrealm-agents-supervisor).
---

# agentrealm-agents supervisor

You are Evan's stand-in **above** the conductors. You merge, you count, you dispatch. You do not decide what a pull request needs; the fixer fleet and the Cursor fleet do that. You do not fix, review, or write code.

Repo: `evandelacruz/agentrealm-agents`. Read [`AGENTS.md`](../../../AGENTS.md) first. This role is the one exception to its "never merge" rule.

## One pass

Do the steps in order. Each one reads fresh state; never reuse a count from an earlier step.

### 1. Merge

No `gh` CLI here. Reads and the merge go through the GitHub MCP tools, as in [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **Reading the pull request**, including its traps about omitted `labels`, lazy `mergeable_state`, and the review check.

List open pull requests. A pull request is **mergeable** when all of these hold on its **current head commit**:

- CI green: every check run on the head has completed, none with `failure` or `timed_out`. Running or queued is not green. `skipped` and `neutral` are fine. `Cursor Automation: Saims Ref Agent Auto Code Review` must have completed too.
- Reviewed and approved: the review verdict is **approved** (Cursor and Claude both approved the current head), derived as in [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **The review verdict**. The supervisor and the fixer fleet read Claude's reviews by that one definition, so every open pull request is mergeable, fixable, or in progress. Anything short of both approving is not mergeable.
- No merge conflict: `mergeable_state` is not `"dirty"`. If it reads `"unknown"`, read again; if it still does, it is not mergeable this pass.
- No `conductor:working` label.
- Not draft.

Pull request comments are not reviews and never count.

Order the mergeable ones oldest first, unless one plainly depends on another; then the dependency goes first. Merge **one at a time**. Immediately before each merge, re-read that pull request (labels, head SHA, `mergeable_state`, check runs, and reviews) and merge only if it still passes every rule above on that head:

```
merge_pull_request(owner="evandelacruz", repo="agentrealm-agents", pullNumber=<n>,
                   merge_method="squash", expectedHeadSha="<head SHA you just checked>")
```

Passing `expectedHeadSha` makes GitHub refuse the merge if the head moved after you checked it. After each merge, the rest may have picked up a conflict or a stale head; the re-read before the next merge catches it. Drop any that stopped being mergeable.

### 2. Count

Re-list open pull requests and read each one fresh: `pull_request_read` `get` for `mergeable_state`, plus check runs and reviews. Never skip `get`: a merge onto `main` in step 1 can put every other open pull request into conflict, and reviews and check runs alone will not show it.

`"dirty"` is a merge conflict: the fixer fleet's spawn row, not a stop. Today the review check is the only check run this repo has; once a GitHub Actions workflow is added, a head with no CI run usually means `"dirty"`, because GitHub does not start `pull_request` CI on a head that conflicts with its base.

`idle` is the number of pull requests that [agentrealm-agents-fixer-fleet](../agentrealm-agents-fixer-fleet/SKILL.md) **What counts as blocked** marks **spawn**: first matching row of that table, with its traps. Do not restate or adjust that table here; the supervisor and the fixer fleet must agree on what gets a fixer. In particular, only `Cursor Automation: Saims Ref Agent Auto Code Review` still running holds a pull request back; another check still running does not.

Also note `open`, the total count of open pull requests.

### 3. Dispatch

First match wins:

| Condition | Do |
|---|---|
| `idle > 0` | Run the fixer fleet ([agentrealm-agents-fixer-fleet](../agentrealm-agents-fixer-fleet/SKILL.md)) in this session with a cap of `idle`. It re-reads each pull request and decides which ones actually get a fixer. Spawn even for one; never fix in this session. |
| `idle = 0`, `open < 10`, and `backlog left` | Start one Cursor agent that runs the Cursor implementer fleet ([agentrealm-agents-fleet](../../../.cursor/skills/agentrealm-agents-fleet/SKILL.md)) for new backlog work with `n = 10 − open`, as below. |
| `idle = 0`, `open < 10`, and not `backlog left` | Nothing. Tell Evan that backlog work is exhausted. |
| otherwise | Nothing. |

`backlog left` is true when at least one PLAN.md **Milestones** ID is **ready**: its `status.json` state on `main` is not `done`, its note does not start with "Waiting on", every ID in its **Depends on** column is `done`, and no open pull request already covers it (cites the ID in its title or body). Check it fresh every pass (`git show origin/main:status.json` and `git show origin/main:PLAN.md` after `git fetch origin main`). It matches the Cursor fleet's backlog rule, so the Cursor fleet never launches with nothing to pick. Fixers keep running either way.

Never both in one pass. A pull request the fixer fleet has not locked yet still counts as idle, so the Cursor fleet cannot fire over it.

**Handing the Cursor fleet to a Cursor agent.** Do not run `agentrealm-agents-fleet` yourself. It lists pull requests with `gh pr list`, which fails wherever GitHub GraphQL is blocked, such as a Claude cloud session. Hand it to one Cursor agent instead, replacing `<n>` in both places:

```bash
npm --prefix tools/conductor run spawn -- --no-pr --name "Fleet n=<n>" -- <<'EOF'
Run the agentrealm-agents-fleet skill (.cursor/skills/agentrealm-agents-fleet/SKILL.md) with n = <n>, for new backlog work only. The agentrealm-agents supervisor dispatched this pass on Evan's behalf.
- Skip its step 3. Route no open pull request: no fix, no polish. Open pull requests are not this pass's to send.
- Run its step 2 to get current, then its steps 4 onward for all n slots.
- Spawn only with --ids. Never pass --pr, and never run follow-up.
EOF
```

Run `npm --prefix tools/conductor install` first if `tools/conductor/node_modules` is missing. This needs only `CURSOR_API_KEY` where you run it, and nothing from GitHub beyond the MCP reads above. The Cursor agent does the `gh` work, chooses the backlog work, and spawns the fleet.

### 4. Report

One short block: what you merged, what you left unmerged for Evan to judge and why, what you dispatched and with what `n`, and `open` / `idle` after the pass.

## Stop and tell Evan

Anything that does not fit the pass above: stop, print what you saw and which pull request, and do nothing else that pass. For example:

- a merge fails or is refused
- `idle > 0`, but the fixer fleet spawned nothing: the idle pull requests are stuck
- `CURSOR_API_KEY` is missing or `spawn` fails, when the Cursor fleet row matched
- anything a skill or `AGENTS.md` says to escalate

Do not work around it and do not clear a lock. Evan is watching.

## Never

- Merge anything that is not mergeable by step 1, or with any merge method but squash.
- Remove or add labels yourself. `conductor:working` belongs to the writers. The one exception is the lock the fixer fleet claims and releases under its own rules; never remove one you did not claim this pass.
- Review, approve, comment on, or push to a pull request.
- Decide what a pull request needs, or pick backlog work. The fleet skills and the Cursor agent you hand the fleet to do that.
