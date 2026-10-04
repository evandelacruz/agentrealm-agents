import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { APPROVED_LABEL, CHANGES_REQUESTED_LABEL, REVIEW_CHECK_NAME, WORKING_LABEL } from "./config.js";
import { lockHeld, pullRequestNumber } from "./lock.js";

const execFileAsync = promisify(execFile);

export type OpenPr = {
  number: number;
  title: string;
  url: string;
  headRefName: string;
  headRefOid: string;
  isDraft: boolean;
  reviewDecision: string | null;
  mergeable: string | null;
  mergeStateStatus: string | null;
  statusCheckRollup: Array<StatusCheckRollupItem> | null;
  labels: Array<{ name: string }> | null;
};

/** CheckRun uses `status` + `conclusion`; legacy StatusContext uses `state`. */
type StatusCheckRollupItem = {
  name?: string;
  status?: string;
  state?: string;
  conclusion?: string | null;
};

export type PrCommentSummary = {
  number: number;
  title: string;
  url: string;
  headRefName: string;
  /** Head commit SHA — what a review is "at". Reviews name the commit they read. */
  headSha: string;
  isDraft: boolean;
  reviewDecision: string | null;
  mergeable: string | null;
  mergeStateStatus: string | null;
  hasMergeConflict: boolean;
  unresolvedReviewThreads: number;
  issueComments: number;
  checksOk: boolean | null;
  /** The Cursor review check has not finished. Fixers must leave the PR alone. */
  reviewInProgress: boolean;
  /** Open labels on the PR — carries the conductor in-flight locks. */
  labels: string[];
  /** Commit SHAs that already carry a submitted review (human or agent). */
  reviewedShas: string[];
};

async function ghJson<T>(args: string[]): Promise<T> {
  try {
    const { stdout } = await execFileAsync("gh", args, {
      maxBuffer: 10 * 1024 * 1024,
      env: process.env,
    });
    return JSON.parse(stdout) as T;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new Error(`gh ${args.join(" ")} failed: ${message}`);
  }
}

async function ghText(args: string[]): Promise<string> {
  const { stdout } = await execFileAsync("gh", args, { env: process.env });
  return stdout.trim();
}

export async function listOpenPrs(): Promise<OpenPr[]> {
  return ghJson<OpenPr[]>([
    "pr",
    "list",
    "--state",
    "open",
    "--json",
    "number,title,url,headRefName,headRefOid,isDraft,reviewDecision,mergeable,mergeStateStatus,statusCheckRollup,labels",
    "--limit",
    "50",
  ]);
}

/** Completed CheckRun / StatusContext that should not fail the rollup. */
function rollupItemSucceeded(c: StatusCheckRollupItem): boolean {
  if (c.status !== undefined) {
    const conclusion = c.conclusion ?? "";
    return (
      conclusion === "SUCCESS" ||
      conclusion === "NEUTRAL" ||
      conclusion === "SKIPPED"
    );
  }
  if (c.state !== undefined) {
    return c.state === "SUCCESS";
  }
  return false;
}

/** Still running — must not be read as red (would spawn a spurious fixer). */
function rollupItemPending(c: StatusCheckRollupItem): boolean {
  if (c.status !== undefined) {
    return c.status !== "COMPLETED";
  }
  if (c.state !== undefined) {
    return (
      c.state !== "SUCCESS" &&
      c.state !== "FAILURE" &&
      c.state !== "ERROR"
    );
  }
  return false;
}

export function hasMergeConflict(pr: Pick<OpenPr, "mergeable" | "mergeStateStatus">): boolean {
  return pr.mergeable === "CONFLICTING" || pr.mergeStateStatus === "DIRTY";
}

/** True while the auto code-review check is still running. A label is not involved. */
export function reviewInProgress(pr: Pick<OpenPr, "statusCheckRollup">): boolean {
  return (pr.statusCheckRollup ?? []).some(
    (check) => check.name === REVIEW_CHECK_NAME && rollupItemPending(check),
  );
}

/**
 * Aggregate CI for routing: `true` all green, `false` at least one red,
 * `null` when there are no checks or any check is still in flight.
 *
 * Pending must stay `null` — treating IN_PROGRESS as failure queues fixers
 * for PRs whose smoke job has not finished yet.
 */
export function rollupOk(pr: Pick<OpenPr, "statusCheckRollup">): boolean | null {
  const checks = pr.statusCheckRollup;
  if (!checks || checks.length === 0) return null;
  if (checks.some(rollupItemPending)) return null;
  return checks.every(rollupItemSucceeded);
}

/**
 * GitHub forbids approving your own PR, so agent reviews land as COMMENTED
 * and `reviewDecision` is permanently null here. Verdict labels stand in.
 * A real GitHub decision outranks the labels. `changes-requested` wins when
 * both labels are present.
 */
export function effectiveReviewDecision(
  pr: Pick<PrCommentSummary, "reviewDecision" | "labels">,
): string | null {
  if (pr.reviewDecision !== null) return pr.reviewDecision;
  const labels = new Set(pr.labels);
  if (labels.has(CHANGES_REQUESTED_LABEL)) return "CHANGES_REQUESTED";
  if (labels.has(APPROVED_LABEL)) return "APPROVED";
  return null;
}

export async function summarizeOpenPrs(): Promise<PrCommentSummary[]> {
  const prs = await listOpenPrs();
  if (prs.length === 0) return [];

  const owner = await ghText(["repo", "view", "--json", "owner", "--jq", ".owner.login"]);
  const name = await ghText(["repo", "view", "--json", "name", "--jq", ".name"]);

  const summaries: PrCommentSummary[] = [];
  for (const pr of prs) {
    const [prDetail, issueComments] = await Promise.all([
      ghJson<{
        data: {
          repository: {
            pullRequest: {
              reviewThreads: { nodes: Array<{ isResolved: boolean }> };
              reviews: { nodes: Array<{ state: string; commit: { oid: string } | null }> };
            };
          };
        };
      }>([
        "api",
        "graphql",
        "-f",
        `query=query { repository(owner: "${owner}", name: "${name}") { pullRequest(number: ${pr.number}) { reviewThreads(first: 100) { nodes { isResolved } } reviews(last: 50) { nodes { state commit { oid } } } } } }`,
      ]),
      ghJson<{ comments: unknown[] }>([
        "pr",
        "view",
        String(pr.number),
        "--json",
        "comments",
      ]),
    ]);

    const detail = prDetail.data.repository.pullRequest;
    summaries.push({
      number: pr.number,
      title: pr.title,
      url: pr.url,
      headRefName: pr.headRefName,
      headSha: pr.headRefOid,
      isDraft: pr.isDraft,
      reviewDecision: pr.reviewDecision,
      mergeable: pr.mergeable,
      mergeStateStatus: pr.mergeStateStatus,
      hasMergeConflict: hasMergeConflict(pr),
      unresolvedReviewThreads: detail.reviewThreads.nodes.filter((t) => !t.isResolved).length,
      issueComments: issueComments.comments.length,
      checksOk: rollupOk(pr),
      reviewInProgress: reviewInProgress(pr),
      labels: (pr.labels ?? []).map((l) => l.name),
      reviewedShas: submittedReviewShas(detail.reviews.nodes),
    });
  }
  return summaries;
}

async function prLabelNames(prNumber: number): Promise<string[]> {
  const view = await ghJson<{ labels: Array<{ name: string }> | null }>([
    "pr",
    "view",
    String(prNumber),
    "--json",
    "labels",
  ]);
  return (view.labels ?? []).map((label) => label.name);
}

/**
 * Claim `conductor:working` on an existing PR. Refuses when that label is
 * already present, so a second writer does not start. Adds only this label.
 * Never replaces the label set. Callers must not delete the label first to
 * bypass the refuse — Claude Code fixers use the same lock. A review still
 * running is a GitHub check, not a label.
 */
export async function acquireWorkingLock(prRef: string): Promise<void> {
  const number = pullRequestNumber(prRef);
  if (lockHeld(await prLabelNames(number))) {
    throw new Error(`PR #${number} already has ${WORKING_LABEL}. Not starting another writer.`);
  }
  try {
    await ghText(["pr", "edit", String(number), "--add-label", WORKING_LABEL]);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (message.includes("addLabelsToLabelable") || message.includes("Resource not accessible")) {
      throw new Error(
        `Cannot set ${WORKING_LABEL} on PR #${number}. The GitHub token cannot edit labels. Grant the Cursor GitHub app permission to apply labels, then retry. ${message}`,
      );
    }
    throw err;
  }
}

/** Drop `conductor:working` only. Used when spawn or follow-up fails before the agent starts. */
export async function releaseWorkingLock(prRef: string): Promise<void> {
  const number = pullRequestNumber(prRef);
  await ghText(["pr", "edit", String(number), "--remove-label", WORKING_LABEL]);
}

/**
 * Hold `conductor:working` for the duration of `body`. The label stays if
 * `body` succeeds — the agent removes it after the push. A failure before
 * that releases the label so the PR is not stuck.
 */
async function assertReviewSettled(prRef: string): Promise<void> {
  const number = pullRequestNumber(prRef);
  const view = await ghJson<{ statusCheckRollup: OpenPr["statusCheckRollup"] }>([
    "pr",
    "view",
    String(number),
    "--json",
    "statusCheckRollup",
  ]);
  if (reviewInProgress(view)) {
    throw new Error(
      `PR #${number} has "${REVIEW_CHECK_NAME}" still running. Not starting a fixer.`,
    );
  }
}

export async function withWorkingLock<T>(prRef: string, body: () => Promise<T>): Promise<T> {
  await assertReviewSettled(prRef);
  await acquireWorkingLock(prRef);
  try {
    return await body();
  } catch (err) {
    try {
      await releaseWorkingLock(prRef);
    } catch {
      // The original error is the one to surface. The label may still be set.
    }
    throw err;
  }
}

/**
 * Commit SHAs carrying a *submitted* review. PENDING reviews are drafts the
 * author has not sent, so they must not count as "this commit was reviewed" —
 * treating them as reviewed would silently drop the PR out of the queue.
 */
export function submittedReviewShas(
  reviews: Array<{ state: string; commit: { oid: string } | null }>,
): string[] {
  const shas = new Set<string>();
  for (const review of reviews) {
    if (review.state === "PENDING") continue;
    if (review.commit?.oid) shas.add(review.commit.oid);
  }
  return [...shas];
}
