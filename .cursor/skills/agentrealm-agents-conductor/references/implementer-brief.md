# Implementer brief

The required shape of every spawn and follow-up prompt. A brief missing any of these produces work that has to be redone.

## Required elements

Every brief MUST:

- **Cite exact milestone IDs** from PLAN.md **Milestones**.
- **State the scope:** what *this* PR covers, when the ID is larger than one PR.
- Tell the agent to read [`AGENTS.md`](../../../../AGENTS.md) and the PLAN.md sections the milestone touches before writing code.
- Say: reference the IDs in commits and the PR body.
- Say: the PR description opens with a product sentence. The first text is one or two sentences in plain language: what someone running a reference agent, or the agent itself, can do now, and why it matters. Write it the way a product person would. Leave the mechanism out (no functions, files, types, or a walk through the logic) because the diff already shows that. Milestone IDs and the rest of the detail come after that sentence.
- Say: **open the PR ready for review, not draft.** If tooling defaults to draft, mark it ready before finishing. If a draft already exists, run `gh pr ready`.
- Say: if blocked by an open design question or a server gap, **halt and print why**. Do not invent. Check PLAN.md and the published docs first.
- Say: do not add dependencies or change the call budget or pacing without flagging prominently.
- Say: keep PLAN.md and README.md matching the code, and run `make test` before pushing.

## Partial work

**One milestone may take several PRs.** A work item too large for one reviewable PR ships as declared serial slices. The agent marks the ID:

```json
{"state": "partial", "remaining": "<what is left>"}
```

The next pass picks it back up carrying that note. Do not stretch one PR to cover a whole large ID, and do not treat one ID as one PR by rule.

## Template

```
Implement <IDS> from PLAN.md Milestones.

Scope for this PR: <what this PR covers and what it explicitly does not>

Read first:
- AGENTS.md: boundaries, rules
- PLAN.md: <sections>
- Published docs (https://agentrealm.gg/docs): <sections, when the slice uses API behavior PLAN.md does not describe>

Rules:
- Reference <IDS> in commits and the PR body.
- The PR description opens with a product sentence. The first text is one or two sentences in plain language: what someone running a reference agent, or the agent itself, can do now, and why it matters. Write it the way a product person would. Leave the mechanism out (no functions, files, types, or a walk through the logic) because the diff already shows that. Milestone IDs and the rest of the detail come after that sentence. For example: "A scripted character now walks back to the chest it dropped when it died."
- Open the PR ready for review, not draft.
- Do not add dependencies without flagging prominently.
- Do not change the call budget or pacing without flagging.
- Keep PLAN.md and README.md matching the code. Run make test before pushing.
- If blocked by an open design question or a server gap, halt and print why.
  Check PLAN.md first; most questions are already answered.
- If this scope does not finish <IDS>, set status to
  {"state":"partial","remaining":"..."} in status.json.
- The conductor prompt carries the conductor:working rule. Follow it.
  Remove only that label after the push, and only on the PR this session
  holds. Never replace the label set. Never clear conductor:working on
  another PR.
```

## Follow-up template

```
Address unresolved PR review comments on #<n>. Keep the same milestone IDs.
If you edit the PR description, keep the opening as a product sentence: one or two sentences in plain language about what someone running a reference agent, or the agent itself, can do now, and why it matters. Leave the mechanism out. The diff already shows the logic.
Keep the PR ready for review, not draft; run `gh pr ready` if needed.
Do not reopen the slice or expand scope.
Stop and ask if a comment requires a design decision or a server change, after
checking PLAN.md for an answer that already exists.
```

Pass `--pr` when running follow-up. The CLI claims `conductor:working`. Do not add that label in the prompt.
