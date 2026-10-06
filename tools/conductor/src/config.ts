export const DEFAULT_ENV_NAME = "evandelacruz/agentrealm-agents";
export const DEFAULT_REPO_URL = "https://github.com/evandelacruz/agentrealm-agents";
export const DEFAULT_MODEL = process.env.CURSOR_MODEL ?? "composer-2.5";

/**
 * Composer models default to their `fast` variant. Pin it off: implementers run
 * unattended, so the slower, cheaper variant is the right default.
 * CURSOR_MODEL_FAST=true turns it back on.
 */
export const MODEL_PARAMS = [{ id: "fast", value: process.env.CURSOR_MODEL_FAST === "true" ? "true" : "false" }];

export function modelSelection(id: string = DEFAULT_MODEL): { id: string; params?: { id: string; value: string }[] } {
  // Only Composer models take the `fast` parameter.
  return id.startsWith("composer") ? { id, params: MODEL_PARAMS } : { id };
}
export const DEFAULT_STARTING_REF = "main";

/** The only label with meaning: the writer lock. Verdicts come from reviews, never labels. */
export const WORKING_LABEL = "conductor:working";

/**
 * GitHub check for the Cursor review automation. Not a label. Override with
 * CONDUCTOR_REVIEW_CHECK if the automation is renamed.
 */
export const REVIEW_CHECK_NAME =
  process.env.CONDUCTOR_REVIEW_CHECK?.trim() ||
  "Cursor Automation: Saims Ref Agent Auto Code Review";

/** Cursor's review bot. Its reviews carry a real APPROVED / CHANGES_REQUESTED state. */
export const CURSOR_REVIEWER_LOGIN = "cursor";
/** Claude Code reviews post under the repo owner's account, always as COMMENTED. */
export const CLAUDE_REVIEWER_LOGIN = "evandelacruz";

export function requireApiKey(): string {
  const key = process.env.CURSOR_API_KEY?.trim();
  if (!key) {
    throw new Error(
      "Set CURSOR_API_KEY (https://cursor.com/dashboard/api) before running this command.",
    );
  }
  return key;
}
