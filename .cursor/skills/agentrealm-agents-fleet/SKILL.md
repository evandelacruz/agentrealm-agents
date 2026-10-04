---
name: agentrealm-agents-fleet
description: >-
  Batch-spawn n cloud agents against agentrealm-agents: assign each to one open PR (fix or
  polish) or one chosen backlog item. Use when Evan asks to kick off a fleet,
  spawn n agents in parallel, or run a batch conductor pass.
---

# agentrealm-agents Fleet

Batch counterpart to [agentrealm-agents-conductor](../agentrealm-agents-conductor/SKILL.md). Plans and spawns **`n`** agents. Needs `CURSOR_API_KEY`.

Repo: `evandelacruz/agentrealm-agents`.

## When to use

- Evan gives a number: "spawn 4 agents", "fleet pass", "batch conductor"
- Parallel PR fixes plus new work in one shot

## One fleet pass

1. **Choose `n`** from Evan's request. Not capped at 2.

2. **Get current first.**

   ```bash
   git fetch origin main
   git show origin/main:status.json
   gh pr list --state open --json number,title,labels,mergeable,isDraft,statusCheckRollup
   ```

3. **Route open PRs first.** They fill slots before new work. Evaluate each open PR in order; first match wins:

   | Condition | Outcome |
   |---|---|
   | `conductor:working` label | skip: an implementer or fixer holds it |
   | `Cursor Automation: Saims Ref Agent Auto Code Review` still running | skip: a review is in progress |
   | merge conflict, changes requested, red CI, or unresolved threads without approval | **fix** |
   | approved with open threads | **polish** |
   | approved, no conflicts, nothing open | skip: Evan merges |
   | draft | skip |

   The working-label row always wins. Implementers and fixers claim the lock and must release it when they finish. Reviewers never take it. Cursor `status` listing the original implementer as `finished` means that worker should already have released; if the label is still there, a Claude Code fixer may hold it (they do not appear in that list) or the holder crashed. **Never remove `conductor:working` to "unstick" a PR.** Report it to Evan and fill the slot with other work.

   Fix rules run before the draft check: a draft with a merge conflict is still broken.

   Checks that are still running are **not** failure. Only an explicit failure queues a fixer.

4. **Choose backlog work yourself** for remaining slots. Do not walk PLAN.md **Milestones** in order; it cannot tell a blocked item from a ready one.

   For each candidate read its `status.json` entry, its `note` or `remaining`, its **Depends on** column, and recent merges. Skip anything whose note starts with "Waiting on" or says it is blocked.

5. **Write an assignment per slot.** Each needs IDs, a scope, and a reason:

   ```json
   [
     {
       "ids": ["M5"],
       "scope": "Seed script for an account and a key only. The playable sandbox map stays deferred.",
       "why": "the next item cannot start until it lands."
     }
   ]
   ```

   `scope` is required: it is what **this** PR covers. Assign fewer than `n` when the backlog does not honestly hold that many independent pieces.

6. **Sanity-check, then spawn.** For every new-work ID, confirm it is not already `done` on `origin/main`. Use the brief shape in [implementer-brief.md](../agentrealm-agents-conductor/references/implementer-brief.md).

   ```bash
   npm --prefix tools/conductor run spawn -- --ids A8 --name "Runtime directives" -- <<'EOF'
   <implementer brief>
   EOF
   ```

   Fix / polish on an existing PR: `spawn --pr <url>` or `follow-up --agent bc-… --pr <url>`.

   Both commands claim `conductor:working` and refuse a PR that already has it. They also refuse while `Cursor Automation: Saims Ref Agent Auto Code Review` is still running. Do not add the label yourself before the command, and do not delete it so the command will accept the PR. The agent that holds the lock removes only that label after the push. Skip a locked PR and skip a PR whose review check is still running. Claude Code uses the same label for review fixes, skips that same check, and does not take new backlog slots or review.

7. **Report** to Evan: assignments, agent URLs, skips, and empty slots. Do not merge anything.

## Choosing backlog work

**One ID per agent, one agent per ID.** Backlog items are PR-sized, so an agent takes one item. Never put two slots on the same ID in a batch; if fewer IDs are ready than slots, leave the extra slots empty rather than splitting an item. Milestones (M6, M7, …) are headings, not items: never assign one.

**Hard gates first.** Respect stated dependencies. Prefer work that unlocks other items over parallel leaf work when both are ready.

**Read `note` and `remaining`.** Skip items whose note starts with "Waiting on" or says blocked. Those are Evan's to resolve, not an implementer's. Skip umbrella items, whose work lives in their child IDs.

**Ready items.** Choose work from PLAN.md **Milestones**: any item ID whose `status.json` state is not `done` and whose **Depends on** IDs are all `done`, minus the items the rule above skips and any an open pull request already covers (cites the ID in its title or body). If no ready work is left for a slot, leave it empty. If every slot is empty, spawn nothing and say that ready backlog work is exhausted.

**One reviewable PR per agent.** Split a large item with an explicit scope and leave it `partial`.

**Prefer non-overlapping modules** across the batch. A smaller vertical slice that merges clean beats a wide PR that fights every sibling. Conflict avoidance applies to new work only. Open PRs are never skipped for overlap.

## Out of scope

- Auto-merge. Never.
- Inventing stack, architecture, or policy answers.
- Spawning without `CURSOR_API_KEY`.
- Clearing `conductor:working`. That lock is shared. Only the holder releases it.
