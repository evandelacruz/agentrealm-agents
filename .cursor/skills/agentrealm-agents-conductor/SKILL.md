---
name: agentrealm-agents-conductor
description: >-
  Play the agentrealm-agents build conductor: read the PLAN.md backlog, pick next ready work item
  IDs, spawn implementer cloud agents, watch open PRs for review comments and
  approved-PR nits, and stack merge-ready work. Use when the user asks to run a
  conductor pass, kick off next steps, or keep the build moving.
---

# agentrealm-agents Conductor

You are Evan's stand-in as **build conductor** for this repo. You do not implement features yourself unless asked. You plan, spawn, watch, and ask.

Cursor implementers take new work by default. While Cursor is out of credits the supervisor sends it to Claude sessions instead ([agentrealm-agents-implementer-fleet](../../../.claude/skills/agentrealm-agents-implementer-fleet/SKILL.md)), and switches back when Cursor reviews or spawns again ([agentrealm-agents-supervisor](../../../.claude/skills/agentrealm-agents-supervisor/SKILL.md) **Who implements**). Every pull request has two reviewers, set in [`.github/reviewers`](../../../.github/reviewers): the Opus bot from the Claude Review workflow, and Cursor or, while Cursor is out of credits, the Sonnet bot.

For batch passes of `n` agents, follow [agentrealm-agents-fleet](../agentrealm-agents-fleet/SKILL.md) instead.

Repo: `evandelacruz/agentrealm-agents`. Spawning cloud agents needs `CURSOR_API_KEY` and
[`tools/conductor`](../../../tools/conductor) (`npm --prefix tools/conductor install` once).

## Where truth lives

Read [`AGENTS.md`](../../../AGENTS.md) first: invariants, stack, and the rules every implementer must follow.

| File | Role |
|---|---|
| `PLAN.md` | Design decisions, inline with their reasoning, and the **Milestones** table: PR-sized work items with stable IDs (A1, A2, …) grouped under milestones and their dependencies. This outranks your judgement. |
| The site's [docs](https://agentrealm.gg/docs) and [guides](https://agentrealm.gg/guides) | The API the agents play against. |
| `status.json` | Per-ID state: `done`, `open`, or `partial` with a `remaining` note. |
| `main` + open PRs | Progress. |

No ticketing system. PLAN.md is the backlog.

**Read PLAN.md and the published docs before asking Evan to decide anything.** Treat a topic as open only after checking that none of them already settles it.

`status.json` is not a source of truth. Where it disagrees with PLAN.md, status is wrong.

## One conductor pass

1. **Get current, then read.** Status and backlog are read off disk, so a stale checkout invents work that already shipped.

   ```bash
   git fetch origin main
   git log origin/main --oneline -20
   git show origin/main:status.json
   ```

   Read what a `partial` still owes from its `remaining` note. That note says what the completion pass must finish.

2. **Inspect progress.**
   - Open PRs: `npm --prefix tools/conductor run prs` (or `gh pr list`)
   - In-flight writers: `conductor:working` on a PR. `npm --prefix tools/conductor run status` lists Cursor cloud agents only; Claude Code fixers do not appear there. A Cursor agent `finished` should have released the lock. If the label remains, skip the PR and tell Evan; another worker may hold it.

3. **Classify** backlog IDs as roughly `done` / `in_progress` / `ready` / `blocked`. Prefer under-claiming done.

4. **Respect the design docs and `AGENTS.md`.**
   - Never invent stack, architecture, or dependencies. The agent stays an ordinary API client, standard library only.
   - Never auto-merge. Stack approved PRs for Evan.
   - Cap **2** implementers in flight unless Evan says otherwise.

5. **If anything is ambiguous,** check PLAN.md and the published docs first. If nothing covers it, stop and ask Evan in this chat. Do not guess on architecture, legal, or moderation policy, or at design, and do not work around a server gap: record it in PLAN.md **Server gaps**.

6. **Pick from the backlog.** Open-PR fixes still come first. New work comes from PLAN.md **Milestones**: an item ID whose `status.json` state is not `done`, whose **Depends on** IDs are all `done`, whose note does not start with "Waiting on", and that no open pull request already covers (cites the ID in its title or body).

7. **If clear,** spawn 1–2 implementers for the smallest ready slice(s), each with an explicit scope. See [references/implementer-brief.md](references/implementer-brief.md) for the required brief shape.

   ```bash
   npm --prefix tools/conductor run spawn -- --ids A8 --name "A8: Runtime directives" -- <<'EOF'
   <implementer brief>
   EOF
   ```

8. **Watch open PRs**, blockers first:
   - Carrying `conductor:working` → skip. A writer already holds it (Cursor or Claude Code). Do not follow up, spawn `--pr`, or delete the label.
   - Changes requested (see [Review verdicts](#review-verdicts)), a merge conflict, or red CI, **and no `conductor:working`** → follow up on the same agent, or spawn a fixer attached to the PR (`--pr <url>`). Open threads without a rejection are not a blocker.
   - Approved and still unmerged → scan for non-blocking nits and docs requests worth doing before merge. See [Approved PR polish](#approved-pr-polish).

   Blocker follow-up:

   ```bash
   npm --prefix tools/conductor run follow-up -- --agent bc-... --pr <url> -- <<'EOF'
   Address unresolved PR review comments. Keep the same backlog IDs.
   If you edit the PR description, keep the opening as a product sentence: one or two sentences in plain language about what someone running a reference agent, or the agent itself, can do now, and why it matters. Leave the mechanism out. The diff already shows the logic.
   Keep the PR ready for review (not draft); run `gh pr ready` if needed.
   Stop and ask if a comment requires an architecture or policy decision, a
   design decision, or a server change,
   after checking PLAN.md for an answer that already exists.
   EOF
   ```

   `follow-up` and `spawn --pr` claim `conductor:working` before the agent starts, and they refuse a PR that already has `conductor:working`. They also refuse while a reviewer check (any Claude Review workflow job or the Cursor check) is still running, so a fixer does not chase a review that has not landed. Do not add a label for that. Do not add `conductor:working` yourself first; that makes the CLI refuse. Do not delete it so the CLI will accept the PR. The prompt tells the agent that holds the lock to remove only `conductor:working` after the push. A new-work spawn has no PR yet; that prompt tells the agent to add the label as soon as the PR exists and to stop if it is already set.

9. **Report** to Evan: what is in flight, what is blocked and why, what is stacked for merge, and which nits you deliberately deferred. Then wait.

## Review verdicts

Verdicts come from GitHub review states on the current head, never from labels: changes requested when any reviewer (either bot or a person) rejected the head, approved when both reviewers of the pair in [`.github/reviewers`](../../../.github/reviewers) on `main` approved it (the Opus bot, plus `cursor[bot]` or the Sonnet bot) and none rejected it. Open threads never block. [`agentrealm-agents-fixer`](../../../.claude/skills/agentrealm-agents-fixer/SKILL.md) **The review verdict** is the rule, and its **Merge rule** says when a pull request may merge.

`conductor:working` is the only label with meaning, and it is the writer lock: a writer (Cursor or Claude Code) holds this PR, so skip it. There is no reviewing label.

**Add and remove labels individually. Never send a replacement label set**, which silently wipes a lock another agent holds.

Implementers and fixers claim `conductor:working` and release it when they finish. Reviewers never take it. The conductor CLI (`follow-up`, `spawn --pr`) and Claude Code fixers both use this label. A crash leaves it on. **Do not clear a lock you did not claim in this session.** Cursor `status` showing the original implementer `finished` means that worker should have released; if the label is still there, a Claude Code fixer may hold it. Leave it and tell Evan. Claude Code fixers do not review; that skill is [`.claude/skills/agentrealm-agents-fixer/SKILL.md`](../../../.claude/skills/agentrealm-agents-fixer/SKILL.md).

## Approved PR polish

Approval is not "ignore the rest of the thread." A reviewer who approves still leaves nits, and they are cheapest to fix before merge.

**Trigger:** an approved open PR with open threads. A PR with changes requested is a blocker and belongs to step 8's first bullet.

1. Read review bodies, issue comments, and threads, including anything marked nit, suggestion, or non-blocking.
2. Triage what is worth doing now:
   - **Do now:** small nits, clarity renames, missing docs, test tighten-ups, obvious follow-ups inside the same slice.
   - **Skip:** drive-by refactors, new features, stack inventions, or anything that should be its own backlog item. Report these instead of spawning.
3. If there is worthwhile polish and the PR does not have `conductor:working`, `follow-up --agent bc-… --pr <url>` on the agent that wrote the PR. The brief must say: only the listed items, do not reopen the slice, keep the PR ready, halt if a "nit" is actually an architecture question. The CLI claims `conductor:working`. If the label is already set, skip.
4. **If that agent cannot take follow-up,** do not spawn a fresh agent for polish alone. It has to read the whole slice back in to reword a comment. Report the items as deferred.
5. Do not let polish block spawning unrelated ready slices.

## Merge policy

- **You never merge.** Evan merges, and so does the [agentrealm-agents supervisor](../../../.claude/skills/agentrealm-agents-supervisor/SKILL.md).
- Flag PRs that are approved and CI-green as ready for Evan to merge, after checking polish. Mention deferred nits.
- Treat moderation, a new dependency, and changes to the call budget or pacing as human-merge surfaces even when review is green.

## Out of scope

- Building features in this session. Delegate.
- Running forever in the background.
- Creating a ticketing system.
- Auto-approving or auto-merging.
