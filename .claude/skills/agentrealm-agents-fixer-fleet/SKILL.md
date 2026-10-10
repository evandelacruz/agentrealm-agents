---
name: agentrealm-agents-fixer-fleet
description: >
  Spawn one Claude session per blocked agentrealm-agents pull request: a review
  requested changes, the branch conflicts with main, or CI is red. Use when
  Evan asks to send fixers at every PR, fan out fixers, or run a fixer
  fleet. Claims conductor:working per PR before spawning. Does not fix,
  review, or merge anything itself.
---

# agentrealm-agents fixer fleet

Batch counterpart to [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md). Survey the open pull requests, claim the lock on each blocked one, spawn one session per pull request, report. **You fix nothing yourself.**

Repo: `evandelacruz/agentrealm-agents`. Read [`AGENTS.md`](../../../AGENTS.md) before spawning. The briefs you write carry its rules.

For a single pull request, use [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) in this session instead. Spawning one session to do one fix is slower than doing it here.

## What counts as blocked

`n` is not a number Evan picks. It is however many open pull requests are blocked. Honor a cap if he gives one, longest-blocked first.

First row that matches decides it:

| Condition | Outcome |
|---|---|
| has `conductor:working` | **skip**: an implementer or fixer holds it and releases it when they finish. Never delete the label to spawn. |
| draft | **skip**: still being written |
| the Claude Review `review` check still running on the head | **skip**: a review is in progress |
| `mergeable_state` is `"dirty"` | **spawn**: merge conflict |
| a CI check run (not the `review` check) concluded `failure` or `timed_out` | **spawn**: red CI |
| a review requested changes | **spawn**: review |
| unresolved threads, and not approved | **spawn**: review |
| approved, only open nit threads | **skip**: nothing blocks the merge |
| green, no conflict, no review yet | **skip**: waiting on the reviewer |

The three spawn rows are not exclusive. A pull request that conflicts **and** is red **and** has threads is one session whose brief carries all three.

Traps that make you spawn at nothing:

- `mergeable_state` reads `"unknown"` on a first fetch. Read it again. `"dirty"` is a conflict; `"unstable"` is a pending or failing check, not a conflict.
- Still-running, `skipped`, and `neutral` checks are not failure.
- The Claude Review workflow's `review` job is a check run, but not CI. A failed or cancelled one is no blocker to spawn for; the supervisor reports it to Evan.
- Claude reviews as `reviewer-agent-anth[bot]` with a real `APPROVED` or `CHANGES_REQUESTED` state; [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) **The review verdict** states the rule. Its inline comments come as extra `COMMENTED` reviews; those are threads, not a verdict. Reviews posted as `evandelacruz` or `cursor[bot]` are not a verdict either.
- Verdicts come from review states, never from labels. [agentrealm-agents-fixer](../agentrealm-agents-fixer/SKILL.md) states the derivation; keep it in that one place. `conductor:working` is a writer lock shared with Cursor. Do not delete it to "unstick" a pull request.

## The pass

**1. Survey.**

```
list_pull_requests(owner="evandelacruz", repo="agentrealm-agents", state="open",
                   fields=["number","title","draft","labels","head","html_url","updated_at"])
```

A pull request with no labels comes back with **no `labels` key at all**.

Then per pull request: `pull_request_read` `get` for `mergeable_state`, `get_check_runs` for CI, `get_reviews` and `get_review_comments` for the review. Record which blockers each one has; the brief needs them.

**2. Claim the lock, one at a time.** You claim it, not the session you spawn. `issue_write` **replaces** the whole label set, so send the existing labels plus the new one:

```
issue_write(method="update", owner="evandelacruz", repo="agentrealm-agents",
            issue_number=<n>, labels=[<existing labels>..., "conductor:working"])
```

Read the labels back. If `conductor:working` was already there, you did not claim it. Skip it and spawn nothing. If the spawn then fails, **release the lock you just claimed**. A lock with no agent behind it stalls the next pass.

**3. Spawn one session per pull request.**

```
create_session(
  source_url="https://github.com/evandelacruz/agentrealm-agents",
  source_revision="<head ref>",
  outcome_branch="<head ref>",
  title="fixer: PR #<n>: <short title>",
  tags=["agentrealm-agents-fixer-fleet", "pr-<n>"],
  prompt="<brief>")
```

Both `source_revision` and `outcome_branch` are the pull request's **own head ref**. That puts the session on the branch the pull request tracks and pushes it back there, which is what stops a fixer opening a second pull request.

Omit `environment_id` so the session inherits this one's. Never pass `permission_mode: "plan"`: it blocks on an approval nobody is waiting to give.

One session per pull request, never two. Never spawn for one you skipped.

**4. Report** to Evan: which pull requests got a fixer and what each was blocked on, the session ID for each, and every skip with its reason. Check on them later with `list_sessions(tags=["agentrealm-agents-fixer-fleet"])`.

## The brief

Carry the pull request, the lock rule, the branch rule, **every blocker you found**, and the halt rule. Include only the blocker sections that apply.

```
Fix pull request #<n> on evandelacruz/agentrealm-agents: <title>
<html_url>

Follow the agentrealm-agents-fixer skill at .claude/skills/agentrealm-agents-fixer/SKILL.md.
Read AGENTS.md first, then the PLAN.md sections a review thread cites.

One pass: apply the fixes and push. Write nothing to GitHub except the
label and a comment on anything you deliberately leave unfixed. No reviews,
no approvals, no resolving threads, no merging, no re-running CI.

The conductor:working label is already claimed for you. Do not claim it
again. After your push, remove only that label, keeping every other one.

You are on <head ref>, the PR's own branch. Push there with
git push -u origin <head ref>. Do not force-push. Do not open a second PR.

Blocking this PR:

[merge conflict] Merge origin/main in and resolve. Never rebase or
force-push. Regenerate tools/conductor/package-lock.json with npm; never
hand-edit it. In status.json keep the more advanced state per ID.

[red CI] <check name> failed: <one line from the log>.
Run <run_id>: pull the output with get_job_logs(failed_only=true).
Reproduce locally, fix, confirm. Never skip or disable a test.

[review] Unresolved threads:
- <path>:<line>: <what it asks, one line>
- …

Keep the same backlog item IDs (<IDS>) and cite them in the commit. Do not
expand the slice. Flag prominently if you add a dependency or touch
moderation or the call budget or pacing.

The blockers above are where to start, not the boundary of what is wrong.
Read the whole diff yourself and keep the point of the change in view.
Check its behavior against the PLAN.md sections its item IDs point
to, not only against the threads. Fix what you find only when it is in
this slice, clearly wrong, and small; say so in the commit. Comment on
anything larger or arguable instead.

Run make test before pushing, and make conductor-test if you touch
tools/conductor.

If a thread needs an architecture, legal, or moderation decision,
a design decision PLAN.md does not settle, or a server change, say so on that thread, leave the label in place, stop, and report
back. Check PLAN.md first; most questions are already answered.
```

Give the threads and the failing check in the brief rather than sending the session to find them. You already read them, and a session that re-derives can read a resolved thread as open or a running check as failed. Listing them precisely frees the session to spend its judgement on the rest of the diff.

## Out of scope

- Fixing anything yourself. You spawn; the sessions fix.
- Merging, reviewing, approving, resolving threads, commenting on a pull request.
- Backlog work, and polish on approved pull requests that nothing blocks.
- Spawning for a locked or draft pull request.
