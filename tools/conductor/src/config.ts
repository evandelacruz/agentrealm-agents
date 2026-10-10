import type { ModelSelection } from "@cursor/sdk";

export const DEFAULT_ENV_NAME = "evandelacruz/agentrealm-agents";
export const DEFAULT_REPO_URL = "https://github.com/evandelacruz/agentrealm-agents";
export const DEFAULT_MODEL = process.env.CURSOR_MODEL ?? "composer-2.5";

/**
 * Composer models default to their `fast` variant. Pin it off: implementers run
 * unattended, so the slower, cheaper variant is the right default.
 * CURSOR_MODEL_FAST=true turns it back on.
 *
 * Which models take `fast`: `Cursor.models.list()` (checked 2026-10-06) lists a
 * `fast` parameter on `composer-2.5` and `composer-2` only.
 */
export function modelSelection(
  id: string = DEFAULT_MODEL,
  env: NodeJS.ProcessEnv = process.env,
): ModelSelection {
  if (!id.startsWith("composer")) return { id };
  return { id, params: [{ id: "fast", value: env.CURSOR_MODEL_FAST === "true" ? "true" : "false" }] };
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

/** The Claude Review workflow (`.github/workflows/claude-review.yml`) and its job. */
export const CLAUDE_REVIEW_WORKFLOW = "Claude Review";
export const CLAUDE_REVIEW_CHECK_NAME = "review";

export function requireApiKey(): string {
  const key = process.env.CURSOR_API_KEY?.trim();
  if (!key) {
    throw new Error(
      "Set CURSOR_API_KEY (https://cursor.com/dashboard/api) before running this command.",
    );
  }
  return key;
}
