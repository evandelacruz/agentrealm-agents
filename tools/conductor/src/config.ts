export const DEFAULT_ENV_NAME = "evandelacruz/agentrealm-agents";
export const DEFAULT_REPO_URL = "https://github.com/evandelacruz/agentrealm-agents";
export const DEFAULT_MODEL = process.env.CURSOR_MODEL ?? "composer-2.5";
export const DEFAULT_STARTING_REF = "main";

export const APPROVED_LABEL = "conductor:approved";
export const CHANGES_REQUESTED_LABEL = "conductor:changes-requested";
export const WORKING_LABEL = "conductor:working";

/** GitHub check for the Cursor review automation. Not a label. */
export const REVIEW_CHECK_NAME = "Cursor Automation: Saims Ref Agent Auto Code Review";

export function requireApiKey(): string {
  const key = process.env.CURSOR_API_KEY?.trim();
  if (!key) {
    throw new Error(
      "Set CURSOR_API_KEY (https://cursor.com/dashboard/api) before running this command.",
    );
  }
  return key;
}
