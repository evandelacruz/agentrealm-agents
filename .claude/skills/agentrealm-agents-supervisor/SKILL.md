---
name: agentrealm-agents-supervisor
description: >
  Stand in for Evan above the agentrealm-agents conductors: merge what is ready, send a
  Claude fixer fleet at idle pull requests, and top up with new work when
  nothing is idle: the Cursor implementer fleet by default, the Claude
  implementer fleet while Cursor is out of credits. Use when Evan asks to supervise,
  run the supervisor, take over merging and dispatch, or loop it
  (/loop agentrealm-agents-supervisor).
---

# agentrealm-agents supervisor

You are Evan's stand-in **above** the conductors. You merge, you count, you dispatch. You do not decide what a pull request needs or pick backlog work; the fleets do that. You do not fix, review, or write code.

Repo: `evandelacruz/agentrealm-agents`. Read [`AGENTS.md`](../../../AGENTS.md) first. This role is the one exception to its "never merge" rule.

## Who reviews

The Claude Review workflow (`.github/workflows/claude-review.yml`) reviews every ready pull request as `reviewer-agent-anth[bot]`, with a real `APPROVED` or `CHANGES_REQUESTED` review. It needs repo secrets `CLAUDE_REVIEWER_APP_ID`, `CLAUDE_REVIEWER_APP_PRIVATE_KEY` and `CLAUDE_CODE_OAUTH_TOKEN`, and takes its model from the optional repo variable `CLAUDE_REVIEW_MODEL`. Cursor (`cursor[bot]`, with the `Cursor Automation: Saims Ref Agent Auto Code Review` check) reviews while it has credits, and people may review too. Every reviewer counts the same, by [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **The review verdict**. Nothing in these skills posts reviews. A `review` check run that failed or was cancelled means no Claude review is coming for that head: name it in the report for Evan and carry on with the pass.

## Who implements

New backlog work goes to one of two fleets. The mode says which:

- **Cursor mode** (the default): the Cursor implementer fleet ([agentrealm-agents-fleet](../../../.cursor/skills/agentrealm-agents-fleet/SKILL.md)), handed to one Cursor agent (step 3).
- **Claude mode**: the Claude implementer fleet ([agentrealm-agents-implementer-fleet](../agentrealm-agents-implementer-fleet/SKILL.md)), run in this session. Never try the conductor `spawn` in this mode.

The mode changes nothing else: not the merge rule, not the fixers, not who reviews.

**Switch to Claude mode** on evidence that Cursor is out of credits. Either one is enough:

- a conductor `spawn` fails and its output names credits, a spend limit, or a usage limit;
- on some open pull request, the output of the `Cursor Automation: Saims Ref Agent Auto Code Review` check names a spend or credit limit.

Nothing weaker switches it: the Cursor check cancelled or failing without naming one, a head with no `cursor[bot]` review, or a slow check. Those are **Stop and tell Evan**.

**Switch back to Cursor mode** automatically on evidence that Cursor has credits again. Either one is enough:

- `cursor[bot]` posted an `APPROVED` or `CHANGES_REQUESTED` review on any pull request after the mode turned on (a `COMMENTED` review does not count);
- a conductor `spawn` succeeds.

**Evan can force either mode** by saying so. A forced mode holds until Evan says otherwise; the evidence above does not override it.

**Record it.** The report's first line is the mode: `Mode: Cursor` or `Mode: Claude since <UTC time>: <evidence>`, with `(forced by Evan)` when it is. If this pass runs from a Routine, keep the same line in its prompt with `update_trigger`. Each pass reads the mode from Evan's latest word in this session, else that prompt line, else the last report in this session. With none of them, it is Cursor mode.

## One pass

Do the steps in order. Each one reads fresh state; never reuse a count from an earlier step.

### 0. Mode

Read the mode as **Who implements** says. Then check its switch evidence against the reviews and check runs you read in steps 1 and 2, and switch if one fires (not when Evan forced the mode). A switch takes effect for this pass's dispatch.

### 1. Merge

No `gh` CLI here. Reads and the merge go through the GitHub MCP tools, as in [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **Reading the pull request**, including its traps about omitted `labels`, lazy `mergeable_state`, and reviewer check runs.

List open pull requests. A pull request is **mergeable** when all of these hold on its **current head commit**:

- CI green: every CI check run on the head has completed, none with `failure` or `timed_out`. Running or queued is not green. `skipped` and `neutral` are fine. A head with no CI run at all is not green; step 2 says why that usually means a conflict. Reviewer check runs (the Claude Review `review` job, the Cursor check) are not CI.
- The [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **Merge rule** holds: approved, no rejection, no review in flight. Beyond that, the merge goes through or GitHub refuses it.
- It changes nothing under `.github/workflows/` or `.github/actions/` (see **Never**).
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

**A missing CI run means check for a conflict first.** GitHub does not start `pull_request` workflows on a head that conflicts with its base, so a head with no `python` or `conductor` check run (the `test` workflow's jobs) is usually `"dirty"`. Read `mergeable_state` before treating it as a CI outage. `"dirty"` is a merge conflict: the fixer fleet's spawn row, not a stop.

`idle` is the number of pull requests that [agentrealm-agents-fixer-fleet](../agentrealm-agents-fixer-fleet/SKILL.md) **What counts as blocked** marks **spawn**: first matching row of that table, with its traps. Do not restate or adjust that table here; the supervisor and the fixer fleet must agree on what gets a fixer. 
Also note `open`, the total count of open pull requests, and `inflight`, the Claude implementer sessions still running without a pull request yet. Find them with `list_sessions(tags=["agentrealm-agents-implementer-fleet"])`: a session counts when `get_session` shows its `status_bucket` as `working` or `blocked`, and no open pull request has its branch (the session's `outcome_branch`, `c/<id>-<slug>`) as head. Each fleet session is tagged with its backlog ID too, lower case. Count them in both modes: a session spawned in Claude mode keeps running after the switch back, and it fills a slot just as an open pull request does.

### 3. Dispatch

First match wins:

| Condition | Do |
|---|---|
| `idle > 0` | Run the fixer fleet ([agentrealm-agents-fixer-fleet](../agentrealm-agents-fixer-fleet/SKILL.md)) in this session with a cap of `idle`. It re-reads each pull request and decides which ones actually get a fixer. Spawn even for one; never fix in this session. |
| `idle = 0`, `open + inflight < 10`, and `backlog left` | Cursor mode: hand the Cursor implementer fleet to one Cursor agent with `n = 10 − open − inflight`, as below. Claude mode: run the Claude implementer fleet ([agentrealm-agents-implementer-fleet](../agentrealm-agents-implementer-fleet/SKILL.md)) in this session with the same `n`. Never implement in this session. |
| `idle = 0`, `open + inflight < 10`, and not `backlog left` | Nothing. Tell Evan that backlog work is exhausted. |
| otherwise | Nothing. |

`backlog left` is true when at least one PLAN.md **Milestones** item ID is **ready**. This is the one definition of ready backlog work; [agentrealm-agents-implementer-fleet](../agentrealm-agents-implementer-fleet/SKILL.md) uses it as written. An ID is ready when all of these hold:

- its `status.json` state on `main` is not `done`;
- its note does not start with "Waiting on" and does not say it is blocked;
- it is not an umbrella item, whose work lives in its child IDs;
- every ID in its **Depends on** column is `done`;
- no open pull request covers it (cites the ID in its title or body);
- no `inflight` session (step 2) covers it (tagged with that ID, lower case).

Check it fresh every pass (`git show origin/main:status.json` and `git show origin/main:PLAN.md` after `git fetch origin main`). The first five rules are the Cursor fleet's [**Choosing backlog work**](../../../.cursor/skills/agentrealm-agents-fleet/SKILL.md) rules; the last is one only a Claude session can check, so the Cursor brief below passes those IDs as excluded. Fixers keep running in either mode.

Never both in one pass. A pull request the fixer fleet has not locked yet still counts as idle, so an implementer fleet cannot fire over it.

**Handing the Cursor fleet to a Cursor agent.** Do not run `agentrealm-agents-fleet` yourself: it lists pull requests with `gh pr list`, which fails where GitHub GraphQL is blocked, such as a Claude cloud session. Hand it to one Cursor agent, replacing `<n>` in both places and `<ids>` with the backlog IDs of every `inflight` session (step 2), comma-separated. The Cursor agent cannot see Claude sessions, so that line is the only thing that stops it re-spawning an ID one is mid-way through. Drop the line when `inflight` is empty:

```bash
npm --prefix tools/conductor run spawn -- --no-pr --name "Fleet n=<n>" -- <<'EOF'
Run the agentrealm-agents-fleet skill (.cursor/skills/agentrealm-agents-fleet/SKILL.md) with n = <n>, for new backlog work only. The agentrealm-agents supervisor dispatched this pass on Evan's behalf.
- Skip its step 3. Route no open pull request: no fix, no polish. Open pull requests are not this pass's to send.
- Run its step 2 to get current, then its steps 4 onward for all n slots.
- Spawn only with --ids. Never pass --pr, and never run follow-up.
- Skip <ids>: Claude sessions hold them.
EOF
```

Run `npm --prefix tools/conductor install` first if `tools/conductor/node_modules` is missing. This needs `CURSOR_API_KEY` where you run it. If the spawn fails on credits, switch to Claude mode (**Who implements**) and run the Claude row in this same pass.

### 4. Report

One short block. Its first line is the mode (**Who implements**). Then: what you merged, what you left unmerged for Evan to judge and why (workflow changes, failed or cancelled `review` checks, reviewers who disagree and a fixer could not settle), what you dispatched and with what `n`, and `open` / `idle` / `inflight` after the pass.

## Stop and tell Evan

Anything that does not fit the pass above: stop, print what you saw and which pull request, and do nothing else that pass. For example:

- a merge fails or is refused
- `idle > 0`, but the fixer fleet spawned nothing: the idle pull requests are stuck
- the Claude implementer fleet row matched, but `create_session` fails
- in Cursor mode, `CURSOR_API_KEY` is missing, or `spawn` fails for a reason other than credits
- the Cursor check looks broken without naming credits: `cancelled`, or failed with no `cursor[bot]` review on that head. Name the pull request and ask Evan whether to force Claude mode
- anything a skill or `AGENTS.md` says to escalate

Do not work around it and do not clear a lock. Evan is watching.

A switch of mode on the evidence in **Who implements** is not a stop. Report it at the top and finish the pass.

## Never

- Merge anything that is not mergeable by step 1, or with any merge method but squash.
- Remove or add labels yourself. `conductor:working` belongs to the writers. The one exception is the lock the fixer fleet claims and releases under its own rules; never remove one you did not claim this pass.
- Review, approve, comment on, or push to a pull request.
- Commit or push to `main`, or brief any agent to. If something seems to need a direct push to `main`, stop and tell Evan.
- Merge a pull request that changes anything under `.github/workflows/` or `.github/actions/`. Workflows run from the pull request's own files with the repo's secrets, so such a pull request can steer or forge its own Claude review. Leave it for Evan and say so in the report.
- Decide what a pull request needs, or pick backlog work. The fleet skills, and the Cursor agent you hand the Cursor fleet to, do that.
