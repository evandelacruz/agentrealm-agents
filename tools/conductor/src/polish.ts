import { POLISH_DONE_LABEL, WORKING_LABEL } from "./config.js";
import type { PrCommentSummary } from "./gh.js";

/** What the polish and merge filters read off a PR summary. */
export type PolishFields = Pick<
  PrCommentSummary,
  "isDraft" | "verdict" | "hasMergeConflict" | "checksOk" | "reviewInProgress" | "labels"
>;

/** Approved, no review in flight, not a draft, and no writer holds the lock. */
function settledApproval(s: PolishFields): boolean {
  return (
    !s.isDraft &&
    !s.labels.includes(WORKING_LABEL) &&
    !s.reviewInProgress &&
    s.verdict === "APPROVED" &&
    !s.hasMergeConflict
  );
}

/**
 * Approved, no red CI, unconflicted, unlocked, and not yet polished: the
 * fixer fleet's polish row (.claude/skills/agentrealm-agents-fixer-fleet).
 * A Claude fixer gives it its one polish pass; the conductor never polishes.
 */
export function awaitingPolish(s: PolishFields): boolean {
  return settledApproval(s) && s.checksOk !== false && !s.labels.includes(POLISH_DONE_LABEL);
}

/** Approved, green, unconflicted, unlocked, and polished: the supervisor's merge rule. */
export function mergeReady(s: PolishFields): boolean {
  return settledApproval(s) && s.checksOk === true && s.labels.includes(POLISH_DONE_LABEL);
}
