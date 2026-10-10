---
name: agentrealm-agents-implementer-fleet
description: >
  Spawn one Claude session per ready agentrealm-agents backlog slice, for new
  backlog work while the Cursor implementer fleet cannot run (Cursor out of
  credits). Use when the agentrealm-agents supervisor dispatches new work in
  Claude mode, or when Evan asks Claude Code to spawn implementers or run an
  implementer fleet. Does not implement, review, or merge anything itself.
---

# agentrealm-agents implementer fleet

Claude fallback for the Cursor fleet ([agentrealm-agents-fleet](../../../.cursor/skills/agentrealm-agents-fleet/SKILL.md)), for new backlog work only. Pick up to `n` ready slices, spawn one session per slice, report. **You implement nothing yourself.**

Repo: `evandelacruz/agentrealm-agents`. Read [`AGENTS.md`](../../../AGENTS.md) before spawning. The briefs you write carry its rules.

It routes no open pull request. Blocked pull requests are the [fixer fleet](../agentrealm-agents-fixer-fleet/SKILL.md)'s; approved ones are the supervisor's to merge.

## When to use

- The supervisor's dispatch row for new work in Claude mode ([agentrealm-agents-supervisor](../agentrealm-agents-supervisor/SKILL.md) **Who implements**), with `n = 10 − open − inflight`.
- Evan asks Claude Code for implementers directly. `n` is Evan's number.

In Cursor mode the supervisor hands new work to the Cursor fleet instead. Never run both for the same pass.

## The pass

**1. Get current.**

```bash
git fetch origin main
git show origin/main:status.json
git show origin/main:PLAN.md
```

```
list_pull_requests(owner="evandelacruz", repo="agentrealm-agents", state="open",
                   fields=["number","title","body","head"])
list_sessions(tags=["agentrealm-agents-implementer-fleet"])
```

No `gh` CLI here; GitHub reads go through the MCP tools.

**2. Find ready IDs and in-flight sessions.** Count `inflight` and find **ready** IDs exactly as [agentrealm-agents-supervisor](../agentrealm-agents-supervisor/SKILL.md) defines them: `inflight` in step 2 (**Count**), ready in step 3 (`backlog left`). Do not restate either here; the supervisor and this fleet must agree, or the supervisor keeps dispatching passes with nothing to pick. An ID an in-flight session covers is not ready, so a slice is never spawned twice.

**3. Choose slices**, by the Cursor fleet's **Choosing backlog work** rules, so both fleets pick the same way:

- One PR-sized item per session. If one is still too large, give the session an explicit scope; it leaves the ID `partial`.
- Respect the **Depends on** column. Prefer work that unlocks other items over leaf work when both are ready.
- Read each candidate's `note` and `remaining`. A `partial` ID continues from its `remaining` note.
- Prefer items that touch different modules, across the batch and against open pull requests. A smaller slice that merges clean beats a wide one that fights a sibling.
- Assign fewer than `n` when the backlog does not honestly hold that many independent pieces. If none is ready, spawn nothing and say that ready backlog work is exhausted.

Write each assignment down before spawning:

```json
{"ids": ["A8"], "scope": "Runtime directives only. Persistence stays deferred.", "why": "A9 and A12 depend on it."}
```

`scope` is required: it is what **this** pull request covers.

**4. Sanity-check, then spawn one session per slice.** Re-read `status.json` on `origin/main` and confirm each ID is still not `done`. Pick a new branch per slice, `c/<id>-<short-slug>` in lower case (`c/a8-runtime-directives`), and confirm with `list_branches` that it does not exist yet.

```
create_session(
  source_url="https://github.com/evandelacruz/agentrealm-agents",
  source_revision="main",
  outcome_branch="<branch>",
  title="implementer: <IDS>: <short scope>",
  tags=["agentrealm-agents-implementer-fleet", "<id lower case>"],
  prompt="<brief>")
```

Omit `environment_id` so the session inherits this one's. Never pass `permission_mode: "plan"`: it blocks on an approval nobody is waiting to give. One session per slice, never two.

**5. Report** to Evan: each assignment (IDs, scope, why), branch and session ID, skipped IDs with the reason, and empty slots. Check on them later with `list_sessions(tags=["agentrealm-agents-implementer-fleet"])`.

## The brief

The Cursor [implementer brief](../../../.cursor/skills/agentrealm-agents-conductor/references/implementer-brief.md) lists the required elements; this template carries them. Fill in every `<…>`:

```
Implement backlog item <IDS> from PLAN.md Milestones on
evandelacruz/agentrealm-agents.

Scope for this PR: <what this PR covers and what it explicitly does not>
Why now: <why>

Read first:
- AGENTS.md: boundaries, rules
- PLAN.md: <sections>
- docs/PLAYABLE_AGENT_PLAN.md: <milestone section>
- Published docs (https://agentrealm.gg/docs): <sections, when the slice
  uses API behavior PLAN.md does not describe>
PLAN.md outranks the backlog state. status.json is not a source of truth.

Work:
- Implement the scope, with tests.
- Keep PLAN.md and README.md matching the code.
- In status.json edit only the <IDS> entries, keeping the blank line
  between entries. If this scope does not finish <IDS>, set
  {"state":"partial","remaining":"..."}; when it does, set {"state":"done"}.
- Run make test before pushing, and make conductor-test if you touch
  tools/conductor.

Branch and pull request:
- You are on <branch>, made from main. Push there with
  git push -u origin <branch>. Never force-push.
- Never commit or push to main, not even a status.json change. If the task
  seems to need a direct push to main, stop and report.
- Open one pull request against main with the GitHub MCP tools (no gh CLI),
  ready for review, not draft.
- The PR description opens with a product sentence: one or two sentences in
  plain language on what someone running a reference agent, or the agent
  itself, can do now, and why it matters. Leave the mechanism out. Item IDs
  and detail come after it.
- Reference <IDS> in every commit and in the PR body.
- Never merge. No reviews or approvals of your own PR.
- Put no AI model names in commits, the PR, or files.

Writer lock:
- As soon as the PR exists, read its labels. If conductor:working is
  already set, stop and report: another writer holds it. Otherwise add
  conductor:working with issue_write, sending the existing labels plus it
  (issue_write replaces the whole label set).
- After your last push, with the PR ready for review, remove only
  conductor:working, keeping every other label. If you stop early with the
  label on, say so in your report.
- Never touch conductor:working on any other PR.

Halt rules:
- Do not add dependencies, or change moderation or the call budget or
  pacing, without flagging it prominently in the commit and PR body.
- If blocked by an open architecture, legal, or moderation question, an
  open design question, or a server gap, halt and print why. Check PLAN.md
  and the published docs first; most questions are already answered.
```

## Out of scope

- Implementing anything yourself. You spawn; the sessions implement.
- Open pull requests: fixing, polishing, reviewing, approving, merging, or commenting.
- Claiming `conductor:working` yourself. New work has no pull request yet; each session claims and releases its own.
- Committing or pushing to `main`, or briefing a session to.
- Inventing stack, architecture, or policy answers.
