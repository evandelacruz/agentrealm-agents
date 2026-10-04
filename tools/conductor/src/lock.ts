import { WORKING_LABEL } from "./config.js";

/** True when an implementer or fixer already holds this pull request. Reviewers never take this lock. */
export function lockHeld(labels: readonly string[]): boolean {
  return labels.includes(WORKING_LABEL);
}

/** Accepts `123`, `#123`, or a GitHub pull request URL. */
export function pullRequestNumber(prRef: string): number {
  const raw = prRef.trim();
  const direct = /^#?(\d+)$/.exec(raw);
  if (direct) return Number(direct[1]);
  const fromUrl = /\/pull\/(\d+)\/?$/.exec(raw);
  if (fromUrl) return Number(fromUrl[1]);
  throw new Error(`Not a pull request: ${prRef}`);
}

/**
 * Appended to every spawn and follow-up prompt.
 * When `prRef` is set, the CLI has already applied `conductor:working`.
 */
export function lockNote(prRef: string | undefined): string {
  if (prRef) {
    const number = pullRequestNumber(prRef);
    return [
      "",
      "Conductor lock:",
      `PR #${number} is labeled ${WORKING_LABEL}. You are the only writer on it.`,
      "When the fix is pushed and the PR is ready for review, remove only that label:",
      `gh pr edit ${number} --remove-label ${WORKING_LABEL}`,
      "Do not replace the label set, and do not remove any other label.",
      "Do not remove conductor:working from any other pull request.",
      "If you stop before that push, leave the label in place and say so.",
      "",
    ].join("\n");
  }
  return [
    "",
    "Conductor lock:",
    `As soon as the pull request exists, add ${WORKING_LABEL} (\`gh pr edit <n> --add-label ${WORKING_LABEL}\`).`,
    `If it already has ${WORKING_LABEL}, stop without pushing and report that.`,
    `When the work is pushed and the PR is ready for review, remove only ${WORKING_LABEL} on that PR.`,
    "Add and remove that label on its own. Never send a replacement label set.",
    "Never remove conductor:working from a pull request you did not just open.",
    "If you stop before that push, leave the label in place and say so.",
    "",
  ].join("\n");
}
