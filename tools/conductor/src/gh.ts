import { execFile } from "node:child_process";
import { promisify } from "node:util";
import {
  CLAUDE_REVIEW_WORKFLOW,
  DEFAULT_REVIEWER_PAIR,
  REVIEW_CHECK_NAME,
  REVIEWERS_FILE,
  WORKING_LABEL,
} from "./config.js";
import { lockHeld, pullRequestNumber } from "./lock.js";

const execFileAsync = promisify(execFile);

export type OpenPr = {
  number: number;
  title: string;
  url: string;
  headRefName: string;
  headRefOid: string;
  isDraft: boolean;
  mergeable: string | null;
  mergeStateStatus: string | null;
  statusCheckRollup: Array<StatusCheckRollupItem> | null;
  labels: Array<{ name: string }> | null;
};

/** CheckRun uses `status` + `conclusion`; legacy StatusContext uses `state`. */
type StatusCheckRollupItem = {
  name?: string;
  workflowName?: string;
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
  /** Combined verdict of every reviewer on `headSha` only. See `headVerdict`. */
  verdict: Verdict;
  mergeable: string | null;
  mergeStateStatus: string | null;
  hasMergeConflict: boolean;
  unresolvedReviewThreads: number;
  issueComments: number;
  checksOk: boolean | null;
  /** A reviewer check has not finished. The CLI starts no fixer meanwhile. */
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
    "number,title,url,headRefName,headRefOid,isDraft,mergeable,mergeStateStatus,statusCheckRollup,labels",
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

/** Reviewer checks are not CI: their result is the review they post. */
function isReviewerCheck(c: StatusCheckRollupItem): boolean {
  return (
    c.name === REVIEW_CHECK_NAME || c.workflowName === CLAUDE_REVIEW_WORKFLOW
  );
}

/** True while a reviewer check is still running. A label is not involved. */
export function reviewInProgress(pr: Pick<OpenPr, "statusCheckRollup">): boolean {
  return (pr.statusCheckRollup ?? []).some(
    (check) => isReviewerCheck(check) && rollupItemPending(check),
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
  // Reviewer checks are not CI: a failed review job is no reason for a fixer.
  const checks = (pr.statusCheckRollup ?? []).filter((c) => !isReviewerCheck(c));
  if (checks.length === 0) return null;
  if (checks.some(rollupItemPending)) return null;
  return checks.every(rollupItemSucceeded);
}

export type Verdict = "APPROVED" | "CHANGES_REQUESTED" | null;

/** A submitted or pending review, oldest first, as the GraphQL `reviews` connection returns it. */
export type ReviewNode = {
  state: string;
  body: string;
  author: { login: string } | null;
  commit: { oid: string } | null;
};

/** The two reviewers every PR needs: the Opus bot and the second one, by login. */
export type ReviewerPair = { opus: string; second: string };

/** GraphQL drops the `[bot]` suffix that REST keeps; compare without it. */
function loginKey(login: string): string {
  return login.replace(/\[bot\]$/, "");
}

/**
 * Parse `.github/reviewers`: `key: value` lines, `#` comments. `second` is
 * `cursor` or `sonnet` and names the line holding that reviewer's login.
 */
export function parseReviewers(text: string): ReviewerPair {
  const fields = new Map<string, string>();
  for (const line of text.split("\n")) {
    const m = /^([a-z]+):\s*(\S+)\s*$/.exec(line.trim());
    if (m) fields.set(m[1], m[2]);
  }
  const opus = fields.get("opus");
  const which = fields.get("second");
  if (which !== "cursor" && which !== "sonnet") {
    throw new Error(`${REVIEWERS_FILE}: second must be cursor or sonnet, got ${which ?? "nothing"}.`);
  }
  const second = fields.get(which);
  if (!opus || !second) throw new Error(`${REVIEWERS_FILE}: needs opus and ${which} logins.`);
  return { opus, second };
}

/** The pair on `main`, or `cursor` as the second when the file is missing or malformed, as the workflow does. */
async function loadReviewerPair(owner: string, name: string): Promise<ReviewerPair> {
  try {
    return parseReviewers(
      await ghText([
        "api",
        "-H",
        "Accept: application/vnd.github.raw",
        `repos/${owner}/${name}/contents/${REVIEWERS_FILE}?ref=main`,
      ]),
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.warn(`Cannot read ${REVIEWERS_FILE} on main; second reviewer is cursor. ${message}`);
    return DEFAULT_REVIEWER_PAIR;
  }
}

/**
 * The review verdict on the current head, per the fixer skill. Labels play
 * no part. Each reviewer's latest APPROVED / CHANGES_REQUESTED review on
 * `headSha` is their verdict; COMMENTED reviews are threads. Any reviewer's
 * rejection wins, a person's included; otherwise it is approved only when
 * both of the pair approved; otherwise it is still waiting on a review.
 */
export function headVerdict(reviews: ReviewNode[], headSha: string, pair: ReviewerPair): Verdict {
  const latest = new Map<string, string>();
  for (const r of reviews) {
    if (r.commit?.oid !== headSha) continue;
    if (r.state !== "APPROVED" && r.state !== "CHANGES_REQUESTED") continue;
    latest.set(loginKey(r.author?.login ?? ""), r.state);
  }
  if ([...latest.values()].includes("CHANGES_REQUESTED")) return "CHANGES_REQUESTED";
  const approved = (login: string) => latest.get(loginKey(login)) === "APPROVED";
  if (approved(pair.opus) && approved(pair.second)) return "APPROVED";
  return null;
}

type TriageFields = Pick<
  PrCommentSummary,
  "isDraft" | "verdict" | "hasMergeConflict" | "checksOk" | "reviewInProgress"
>;

/**
 * Sort open PRs for `prs`, per conductor skill step 8. Only a merge conflict,
 * red CI, or a reviewer's changes-requested verdict on the head needs a
 * fixer. Open review threads never do. A PR that needs a fixer appears only
 * there, never also as merge-ready. PRs whose review check is still running
 * appear nowhere.
 */
export function triagePrs<T extends TriageFields>(
  summaries: T[],
): { needsFix: T[]; mergeReady: T[] } {
  const settled = summaries.filter((s) => !s.reviewInProgress);
  const blocked = (s: T) =>
    s.hasMergeConflict || s.checksOk === false || s.verdict === "CHANGES_REQUESTED";
  return {
    needsFix: settled.filter(blocked),
    mergeReady: settled.filter(
      (s) => !blocked(s) && !s.isDraft && s.verdict === "APPROVED" && s.checksOk === true,
    ),
  };
}

export async function summarizeOpenPrs(): Promise<PrCommentSummary[]> {
  const prs = await listOpenPrs();
  if (prs.length === 0) return [];

  const owner = await ghText(["repo", "view", "--json", "owner", "--jq", ".owner.login"]);
  const name = await ghText(["repo", "view", "--json", "name", "--jq", ".name"]);
  const pair = await loadReviewerPair(owner, name);

  const summaries: PrCommentSummary[] = [];
  for (const pr of prs) {
    const [prDetail, issueComments] = await Promise.all([
      ghJson<{
        data: {
          repository: {
            pullRequest: {
              reviewThreads: { nodes: Array<{ isResolved: boolean }> };
              reviews: { nodes: ReviewNode[] };
            };
          };
        };
      }>([
        "api",
        "graphql",
        "-f",
        `query=query { repository(owner: "${owner}", name: "${name}") { pullRequest(number: ${pr.number}) { reviewThreads(first: 100) { nodes { isResolved } } reviews(last: 50) { nodes { state body author { login } commit { oid } } } } } }`,
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
      verdict: headVerdict(detail.reviews.nodes, pr.headRefOid, pair),
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
 *
 * The claim is check-then-add, not atomic: two writers that start within the
 * same moment can both see no label and both claim it.
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
      `PR #${number} has a reviewer check still running. Not starting a fixer.`,
    );
  }
}

/**
 * Run `body` with the lock held. `body` calls `started()` once the agent has
 * been sent its prompt. A failure before that releases the lock so the PR is
 * not stuck. A failure after it (for example `run.wait()` under `--wait`)
 * keeps the lock: the cloud agent is still writing, and it removes the label
 * after its push.
 */
export async function holdLock<T>(
  release: () => Promise<void>,
  body: (started: () => void) => Promise<T>,
): Promise<T> {
  let agentStarted = false;
  try {
    return await body(() => {
      agentStarted = true;
    });
  } catch (err) {
    if (!agentStarted) {
      try {
        await release();
      } catch {
        // The original error is the one to surface. The label may still be set.
      }
    }
    throw err;
  }
}

/**
 * Hold `conductor:working` for the duration of `body`. The label stays if
 * `body` succeeds or fails after the agent starts — the agent removes it
 * after the push. A failure before the agent starts releases the label.
 */
export async function withWorkingLock<T>(
  prRef: string,
  body: (started: () => void) => Promise<T>,
): Promise<T> {
  await assertReviewSettled(prRef);
  await acquireWorkingLock(prRef);
  return holdLock(() => releaseWorkingLock(prRef), body);
}

/**
 * Commit SHAs carrying a *submitted* review. PENDING reviews are drafts the
 * author has not sent, so they must not count as "this commit was reviewed" —
 * treating them as reviewed would silently drop the PR out of the queue.
 */
export function submittedReviewShas(
  reviews: Array<Pick<ReviewNode, "state" | "commit">>,
): string[] {
  const shas = new Set<string>();
  for (const review of reviews) {
    if (review.state === "PENDING") continue;
    if (review.commit?.oid) shas.add(review.commit.oid);
  }
  return [...shas];
}
