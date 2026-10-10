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

Every pull request has exactly two reviewers, set in [`.github/reviewers`](../../../.github/reviewers) on `main`: the Opus Review Agent always, and `second:`, either `cursor` (`cursor[bot]`, through its `Cursor Automation: Saims Ref Agent Auto Code Review` check) or `sonnet` (a second Claude app). The Claude Review workflow (`.github/workflows/claude-review.yml`) runs the Opus review, and the Sonnet review when `main` says `sonnet`. People may review too. Who must approve, and whose rejection blocks, is [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **The review verdict**. Nothing in these skills posts reviews. A Claude Review job that failed or was cancelled means that reviewer's review is not coming for that head: name it in the report for Evan and carry on with the pass.

**The second reviewer follows Cursor's credits, and only Evan flips it.** Never edit `.github/reviewers` or Cursor's settings yourself. Read `second:` from `main` each pass and compare it with the mode (**Who implements**):

- Claude mode (Cursor out of credits) with `second: cursor`: nothing can be approved, since Cursor will not review. Tell Evan, at the top of the report, to set `second: sonnet` in `.github/reviewers` and turn off Cursor's review automation.
- Cursor mode with `second: sonnet` after a switch back: tell Evan to set `second: cursor` and turn Cursor's review automation back on.

Repeat it every pass until the file matches the mode.

**A flip strands open pull requests.** A reviewer reviews a head only when its review is triggered, by a push or a re-run, so after Evan flips `second:` no open head has a review from the new second reviewer, and none can reach Approved until it gets one. Every pass, before step 1, find the open, non-draft pull requests whose current head has no `APPROVED` or `CHANGES_REQUESTED` review from the second reviewer and no reviewer check still queued or running:

- `second: sonnet`: re-run that head's latest Claude Review workflow run (`actions_run_trigger` `method: "rerun_workflow_run"` with its `run_id`, from the `review` check run's `html_url`). The re-run's `pair` job reads `main` again and adds the Sonnet review. Re-run a head once: if its run already has a `review (sonnet)` job, do not re-run it; name it in the report.
- `second: cursor`: nothing here can trigger Cursor's review. Name the pull requests in the report so Evan can.

The report lists the pull requests you re-ran.

## Who implements

New backlog work goes to one of two fleets. The mode says which:

- **Cursor mode** (the default): the Cursor implementer fleet ([agentrealm-agents-fleet](../../../.cursor/skills/agentrealm-agents-fleet/SKILL.md)), handed to one Cursor agent (step 3).
- **Claude mode**: the Claude implementer fleet ([agentrealm-agents-implementer-fleet](../agentrealm-agents-implementer-fleet/SKILL.md)), run in this session. Never try the conductor `spawn` in this mode.

The mode changes nothing else: not the merge rule, not the fixers. Who reviews follows it only through `.github/reviewers`, which Evan flips (**Who reviews**).

**Switch to Claude mode** on evidence that Cursor is out of credits. Either one is enough:

- a conductor `spawn` fails and its output names credits, a spend limit, or a usage limit;
- on some open pull request, the output of the `Cursor Automation: Saims Ref Agent Auto Code Review` check names a spend or credit limit.

Nothing weaker switches it: the Cursor check cancelled or failing without naming one, a head with no `cursor[bot]` review, or a slow check. Those are **Stop and tell Evan**.

**Switch back to Cursor mode** automatically on evidence that Cursor has credits again. Either one is enough:

- `cursor[bot]` posted an `APPROVED` or `CHANGES_REQUESTED` review on any pull request after the mode turned on (a `COMMENTED` review does not count);
- a conductor `spawn` succeeds.

Once Evan has turned Cursor's review automation off, the first can no longer fire and Claude mode never spawns, so Evan usually ends Claude mode by saying so.

**Evan can force either mode** by saying so. A forced mode holds until Evan says otherwise; the evidence above does not override it.

**Record it.** The report's first line is the mode: `Mode: Cursor` or `Mode: Claude since <UTC time>: <evidence>`, with `(forced by Evan)` when it is. If this pass runs from a Routine, keep the same line in its prompt with `update_trigger`. Each pass reads the mode from Evan's latest word in this session, else that prompt line, else the last report in this session. With none of them, it is Cursor mode.

## One pass

Do the steps in order. Each one reads fresh state; never reuse a count from an earlier step.

### 1. Merge

No `gh` CLI here. Reads and the merge go through the GitHub MCP tools, as in [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **Reading the pull request**, including its traps about omitted `labels`, lazy `mergeable_state`, and reviewer check runs.

List open pull requests. A pull request is **mergeable** when all of these hold on its **current head commit**:

- CI green: every CI check run on the head has completed, none with `failure` or `timed_out`. Running or queued is not green. `skipped` and `neutral` are fine. A head with no CI run at all is not green; step 2 says why that usually means a conflict. Reviewer check runs (every Claude Review workflow job, the Cursor check) are not CI.
- The [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **Merge rule** holds: both of the pair in `.github/reviewers` on `main` approved, no trusted reviewer rejected, no review in flight. Beyond that, the merge goes through or GitHub refuses it.
- It changes nothing under `.github/workflows/` or `.github/actions/`, and does not change `.github/reviewers` (see **Never**).
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
Also note `open`, the total count of open pull requests, and `inflight`, implementers still running without a pull request yet, from both fleets. Count both in both modes: work spawned in one mode keeps running after a switch, and it fills a slot just as an open pull request does.

- **Claude sessions.** `list_sessions(tags=["agentrealm-agents-implementer-fleet"])`: a session counts when `get_session` shows its `status_bucket` as `working` or `blocked`, and no open pull request has its branch (the session's `outcome_branch`, `c/<id>-<slug>`) as head. Each fleet session is tagged with its backlog ID too, lower case.
- **Cursor agents.** `npm --prefix tools/conductor run status -- --running-count` gives the running Cursor agents; add it to `inflight`. `npm --prefix tools/conductor run status` lists them by name: a Cursor implementer's name starts with its backlog IDs (`A8: Runtime directives`), and a fleet dispatcher's is `Fleet n=<n>`. A running agent that already opened its pull request is counted twice; that only slows dispatch, which is the safe side. Both need `CURSOR_API_KEY`. Without it, count Claude sessions only and say so in the report; Cursor mode then stops (**Stop and tell Evan**).

### 3. Dispatch

**Mode first.** Read the mode as **Who implements** says. Check its switch evidence against the reviews and check runs you read in steps 1 and 2, and switch if one fires (not when Evan forced the mode). Then dispatch in that mode.

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
- no `inflight` implementer (step 2) covers it: a Claude session tagged with that ID (lower case), or a running Cursor agent whose name starts with it.

Check it fresh every pass (`git show origin/main:status.json` and `git show origin/main:PLAN.md` after `git fetch origin main`). The first five rules are the Cursor fleet's [**Choosing backlog work**](../../../.cursor/skills/agentrealm-agents-fleet/SKILL.md) rules; the Cursor fleet checks the last for running Cursor agents itself, and the Cursor brief below passes the Claude sessions' IDs, which it cannot see. Fixers keep running in either mode.

Never both in one pass. A pull request the fixer fleet has not locked yet still counts as idle, so an implementer fleet cannot fire over it.

**Handing the Cursor fleet to a Cursor agent.** Hand none while a `Fleet n=` agent from an earlier pass is still running: its implementers may not have started yet, so the count above cannot see them. Report that dispatch is waiting on it. Otherwise, do not run `agentrealm-agents-fleet` yourself: it lists pull requests with `gh pr list`, which fails where GitHub GraphQL is blocked, such as a Claude cloud session. Hand it to one Cursor agent, replacing `<n>` in both places and `<ids>` with the backlog IDs of every in-flight Claude session (step 2), comma-separated. The Cursor agent cannot see Claude sessions, so that line is the only thing that stops it re-spawning an ID one is mid-way through. Drop the line when `inflight` is empty:

```bash
npm --prefix tools/conductor run spawn -- --no-pr --name "Fleet n=<n>" -- <<'EOF'
Run the agentrealm-agents-fleet skill (.cursor/skills/agentrealm-agents-fleet/SKILL.md) with n = <n>, for new backlog work only. The agentrealm-agents supervisor dispatched this pass on Evan's behalf.
- Skip its step 3. Route no open pull request. Open pull requests are not this pass's to send.
- Run its step 2 to get current, then its steps 4 onward for all n slots.
- Spawn only with --ids, and start each --name with the slice's IDs ("A8: Runtime directives"). Never pass --pr, and never run follow-up.
- Skip <ids>: Claude sessions hold them.
EOF
```

Run `npm --prefix tools/conductor install` first if `tools/conductor/node_modules` is missing. This needs `CURSOR_API_KEY` where you run it. If the spawn fails on credits, switch to Claude mode (**Who implements**) and run the Claude row in this same pass.

### 4. Report

One short block. Its first line is the mode (**Who implements**), then the second reviewer from `.github/reviewers`, with the flip Evan needs to make when it does not match the mode (**Who reviews**). Then: what you merged, what you left unmerged for Evan to judge and why (workflow changes, failed or cancelled Claude Review jobs, reviewers who disagree and a fixer could not settle), what you dispatched and with what `n`, and `open` / `idle` / `inflight` after the pass.

## Stop and tell Evan

Anything that does not fit the pass above: stop, print what you saw and which pull request, and do nothing else that pass. For example:

- a merge fails or is refused
- `idle > 0`, but the fixer fleet spawned nothing: the idle pull requests are stuck
- the Claude implementer fleet row matched, but `create_session` fails
- in Cursor mode, `CURSOR_API_KEY` is missing, `status --running-count` fails, or `spawn` fails for a reason other than credits
- the Cursor check looks broken without naming credits: `cancelled`, or failed with no `cursor[bot]` review on that head. Name the pull request and ask Evan whether to force Claude mode
- anything a skill or `AGENTS.md` says to escalate

Do not work around it and do not clear a lock. Evan is watching.

A switch of mode on the evidence in **Who implements** is not a stop. Report it at the top and finish the pass.

## Never

- Merge anything that is not mergeable by step 1, or with any merge method but squash.
- Remove or add labels yourself. `conductor:working` belongs to the writers. The one exception is the lock the fixer fleet claims and releases under its own rules; never remove one you did not claim this pass.
- Review, approve, comment on, push to, or resolve threads on a pull request. Re-running a Claude Review run after a flip (**Who reviews**) is the one write you make to a pull request's checks. Fixers resolve the threads they fix; nobody else does.
- Edit `.github/reviewers` or Cursor's review automation. Evan flips them (**Who reviews**).
- Commit or push to `main`, or brief any agent to. If something seems to need a direct push to `main`, stop and tell Evan.
- Merge a pull request that changes anything under `.github/workflows/` or `.github/actions/`. Workflows run from the pull request's own files with the repo's secrets, so such a pull request can steer or forge its own Claude review. Leave it for Evan and say so in the report.
- Merge a pull request that changes `.github/reviewers`. It decides who must approve every later pull request. Leave it for Evan and say so in the report.
- Decide what a pull request needs, or pick backlog work. The fleet skills, and the Cursor agent you hand the Cursor fleet to, do that.
