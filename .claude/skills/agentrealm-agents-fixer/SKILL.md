---
name: agentrealm-agents-fixer
description: >
  Fix one blocked agentrealm-agents pull request and push. A review requested
  changes, the branch conflicts with main, or CI is red. Use when Evan asks
  Claude Code to send a fixer, fix a PR, fix review comments, fix a conflict,
  or get a PR green. Claims and releases conductor:working. Writes only that
  label, the push, and a comment on what it deliberately left alone.
---

# agentrealm-agents fixer

One pass on one blocked pull request: claim the lock, apply the fixes, push, release the lock. That is the whole job.

Repo: `evandelacruz/agentrealm-agents`. Read [`AGENTS.md`](../../../AGENTS.md) before editing. PLAN.md outranks the backlog state. `status.json` is not a source of truth.

## Keep the big picture

The blockers you were handed are symptoms. Before you start typing, work out what this pull request is actually for, and hold that in view the whole way through.

- **The thread is evidence, not a spec.** A reviewer describes a problem and often proposes a fix. The problem is usually real; the proposed fix is one option, sometimes a locally sound one that is wrong for the agent. Check it against PLAN.md and the boundaries in `AGENTS.md` before you take it. If it does not hold up, say so on the thread rather than implementing something you can tell is wrong.
- **Fix the cause, not the symptom.** If the same class of finding shows up in three places, the third one is telling you the first two were not the real problem. A redesign is usually larger than one pass: name it and hand it back rather than starting one here.
- **You are one of several agents in this repo at once.** Other branches are moving under you. Prefer a change that stays inside this slice's modules over one that reaches into code every sibling is also editing.
- **Respect the boundaries.** The agent is an ordinary API client, standard library only, inside the call budget. A fix that needs a side door or a dependency is a sign the fix belongs somewhere else, or is a server gap.
- **Leave the next slice easier, not harder.** Between two fixes that both close the thread, take the one that does not have to be undone when the deferred work lands.

## What you write

Three things, and nothing else: the `conductor:working` label, your commits on the pull request's branch, and a comment on anything you deliberately left alone.

- **No reviews, no approvals.** Never.
- **No resolving review threads.** You answer one; you do not close it.
- **No merging.**
- **No re-running CI.** Your push triggers it.
- **No other labels.** `conductor:working` is the only one you touch.
- **No second pull request**, and no force-push.

### Commenting

Comment on what you did **not** fix, so the next reviewer is not left guessing. One reply on the thread, one or two sentences, saying which of these it is:

- already addressed by a commit after the review,
- does not reproduce, or is wrong about the code,
- out of this slice, and why,
- needs a decision that is Evan's, not yours,
- something **you** found that nobody asked about, and chose to leave.

Never comment on what you *did* fix. The diff already says that, and a reply restating it is noise. If you fixed everything, you post nothing.

A thread needing an architecture, legal, or moderation decision, a design decision PLAN.md does not settle, or a server change, is the one case where you both comment and stop: say why on the thread, leave the lock in place, and tell Evan here.

Use `add_reply_to_pull_request_comment` with the thread's comment ID.

## What you take

A pull request is yours when it is **open**, **not draft**, does **not** have `conductor:working`, and at least one of these is true:

| Blocker | How you know |
|---|---|
| Merge conflict | `mergeable_state` is `"dirty"` |
| Red CI | a **CI** check run completed with conclusion `failure` or `timed_out` |
| Changes requested | the review verdict, below |
| Unresolved threads | a thread with `is_resolved: false`, and the verdict is not approved |

Several can be true at once. Clear all of them in the one pass.

Not yours: drafts, and approved pull requests whose only open threads are nits. Nothing blocks those from merging.

One pull request per pass. If asked for several, finish one before locking the next.

## Reading the pull request

No `gh` CLI here. Reads go through the GitHub MCP tools.

| Need | Call |
|---|---|
| Open pull requests, with labels | `list_pull_requests` with `state: "open"`, `fields: ["number","title","draft","labels","head"]` |
| Merge state | `pull_request_read` `method: "get"` |
| Review verdicts | `pull_request_read` `method: "get_reviews"` |
| Review threads | `pull_request_read` `method: "get_review_comments"` |
| Comments on the pull request | `pull_request_read` `method: "get_comments"` |
| CI result | `pull_request_read` `method: "get_check_runs"` |
| Failing job output | `get_job_logs` with `run_id`, `failed_only: true`, `return_content: true` |
| Reply on a thread you left alone | `add_reply_to_pull_request_comment` |

Five things these tools do that will mislead you:

- **`labels` is omitted, not empty.** No `labels` key means no labels.
- **`issue_read` does not resolve pull request numbers.** Labels come from `list_pull_requests`.
- **`mergeable_state` is lazy.** It reads `"unknown"` on a first fetch, so read again. `"dirty"` is a conflict. `"unstable"` is a pending or failing check, **not** a conflict. `"behind"` just means the base moved.
- **Still-running, `skipped`, and `neutral` checks are not failure.**
- **Reviewers post check runs of their own,** the Claude Review workflow's `review` job and `Cursor Automation: Saims Ref Agent Auto Code Review`. They are not CI (see **Red CI** below), and running or red, they never hold a pull request back.

**The review verdict.** There is no `reviewDecision` field; read the reviews. Any reviewer counts: the Claude Review bot (`reviewer-agent-anth[bot]`), `cursor[bot]`, or a person. For each reviewer, take their latest `APPROVED` or `CHANGES_REQUESTED` review on the current head. `COMMENTED` reviews are threads, not a verdict; that includes Claude Code reviews posted as `evandelacruz`. Reviews on an older head do not count.

- **Changes requested**: any reviewer rejected the current head.
- **Approved**: at least one reviewer approved the current head, and none rejected it.
- Otherwise it is waiting on a review, not blocked.

Act on rejecting reviews and their threads, whoever posted them. When reviewers disagree, address the blocking findings, or reply on the thread saying why a finding does not apply. No review check has to complete.

A review on an older head still leaves its inline threads, and they are read like any other.

The first review's threads are often resolved while a later review's are not. Read the threads, not just the newest review body.

## Lock

```
list_pull_requests(owner="evandelacruz", repo="agentrealm-agents", state="open",
                   fields=["number","draft","labels","head"])
```

If `conductor:working` is present, skip that pull request. Another implementer or fixer holds it. Release the lock when you finish; do not delete a lock you did not claim.

`issue_write` **replaces** the whole label set; there is no add or remove. Send the existing labels plus the new one, or you will wipe someone else's:

```
issue_write(method="update", owner="evandelacruz", repo="agentrealm-agents",
            issue_number=<n>, labels=[<existing labels>..., "conductor:working"])
```

Read the labels again. If `conductor:working` was already set by someone else, stop without editing and say so. Never remove a lock you did not just claim.

When the fleet spawned you, the lock is already claimed for you. Do not claim it again. Still release it at the end.

## The pass

Work on the pull request's own branch:

```bash
git fetch origin <head ref>
git checkout -B <head ref> origin/<head ref>
```

Then take the blockers in this order. A conflict changes what CI runs, so it goes first.

**Merge conflict.** Merge `main` in. Never rebase, amend, or force-push: someone else's checkout has to stay valid.

```bash
git fetch origin main
git merge origin/main
```

Regenerate rather than hand-edit anything generated: `npm --prefix tools/conductor install` for `tools/conductor/package-lock.json`. In `status.json`, keep both sides' states and take the more advanced one per ID. Stop and ask Evan only when both sides changed the same logic and keeping either loses behavior.

**Red CI.** First check it is actually CI. A real CI check has `/actions/runs/<run_id>/job/<job_id>` in its `html_url`; that is also where the run ID comes from. The reviewer checks are not CI: the Cursor check is not a workflow run, and the Claude Review workflow's `review` job runs the reviewer, whose result is the review it posts. A failed or cancelled reviewer check is nothing for you to fix; leave it to the supervisor.

Pull the real output before theorizing. Reproduce locally, fix, confirm. Never skip, disable, or quarantine a test to get green. If a test is flaky and you can make it robust inside this slice, do that; otherwise say so and stop.

**Review threads.** Make the changes they ask for. Keep the same backlog item IDs and do not expand the slice. Anything you leave alone gets a reply, per **Commenting** above. Check PLAN.md before deciding a thread needs Evan; most questions are already answered there.

**Your own pass.** Then read the diff yourself. What you were handed is where to start, not the boundary of what is wrong. A reviewer catches what it catches; you are the one person with the whole change in front of you.

Read all of it, not only the lines the threads point at, and ask:

- Does it match PLAN.md? Read the PLAN.md item and the PLAYABLE_AGENT_PLAN.md milestone section its IDs point to and check the behavior itself (scheduler order, reflex order, call budget, rejection handling), not just what the threads mention. The body describes intent; PLAN.md is the spec.
- Do PLAN.md and README.md still describe what the code does? A behavior change that leaves them describing the old one is a finding.
- Does it do what the pull request body claims? Those drift apart more often than either is wrong.
- Did a changed function leave a caller behind that still assumes the old behavior? Check both directions: callers, callees, the trace, the saved state in `.state/`.
- Is an error swallowed, or a failure path that returns success? These are the findings that cost the most later and are cheapest to catch now.
- Does a test assert the rule, or only the shape? A test that would pass with the bug still in it is not protecting anything.
- Is there a second implementation of something the repo already does once?

**Look broadly, act narrowly.** Fix what you find only when it is inside this slice, clearly wrong, and small. Say what you fixed and why in the commit, since nobody asked for it. Everything else (larger, arguable, or outside the slice) gets a comment and no code, per **Commenting** above. Your own pass must never turn into a rewrite: one unrequested fix that is plainly right is worth more than five that widen the diff and force another review cycle.

## Push

- Run `make test`, and `make conductor-test` if you touched `tools/conductor`.
- Cite the pull request's existing backlog item IDs in the commit message.
- Flag prominently in the commit if you added a dependency or touched the moderation, or the call budget or pacing.

```bash
git push -u origin <head ref>
```

## Unlock

Send the label set **minus** `conductor:working`, keeping everything else:

```
issue_write(method="update", owner="evandelacruz", repo="agentrealm-agents",
            issue_number=<n>, labels=[<existing labels minus conductor:working>])
```

If the pull request had no other labels, that is `labels: []`. If you stop before the push, leave the label in place and tell Evan which pull request still holds it.
