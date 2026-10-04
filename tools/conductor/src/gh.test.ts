import { test } from "node:test";
import assert from "node:assert/strict";
import { REVIEW_CHECK_NAME } from "./config.js";
import {
  claudeBodyVerdict,
  hasMergeConflict,
  headVerdict,
  holdLock,
  type ReviewNode,
  reviewInProgress,
  rollupOk,
  submittedReviewShas,
} from "./gh.js";

test("reviewInProgress is only the auto code-review check while it is running", () => {
  assert.equal(
    reviewInProgress({
      statusCheckRollup: [{ name: REVIEW_CHECK_NAME, status: "IN_PROGRESS", conclusion: null }],
    }),
    true,
  );
  assert.equal(
    reviewInProgress({
      statusCheckRollup: [
        { name: REVIEW_CHECK_NAME, status: "COMPLETED", conclusion: "SUCCESS" },
        { name: "smoke", status: "IN_PROGRESS", conclusion: null },
      ],
    }),
    false,
  );
  assert.equal(reviewInProgress({ statusCheckRollup: null }), false);
});

test("rollupOk is null when any CheckRun is still in progress", () => {
  assert.equal(
    rollupOk({
      statusCheckRollup: [
        { status: "COMPLETED", conclusion: "SUCCESS" },
        { status: "IN_PROGRESS", conclusion: null },
      ],
    }),
    null,
  );
});

test("rollupOk is true when every CheckRun completed successfully", () => {
  assert.equal(
    rollupOk({
      statusCheckRollup: [
        { status: "COMPLETED", conclusion: "SUCCESS" },
        { status: "COMPLETED", conclusion: "SKIPPED" },
        { status: "COMPLETED", conclusion: "NEUTRAL" },
      ],
    }),
    true,
  );
});

test("rollupOk is false when a completed CheckRun failed", () => {
  assert.equal(
    rollupOk({
      statusCheckRollup: [
        { status: "COMPLETED", conclusion: "SUCCESS" },
        { status: "COMPLETED", conclusion: "FAILURE" },
      ],
    }),
    false,
  );
});

test("rollupOk is null with an empty or missing rollup", () => {
  assert.equal(rollupOk({ statusCheckRollup: null }), null);
  assert.equal(rollupOk({ statusCheckRollup: [] }), null);
});

test("rollupOk treats legacy StatusContext PENDING as not yet decided", () => {
  assert.equal(rollupOk({ statusCheckRollup: [{ state: "PENDING" }] }), null);
  assert.equal(rollupOk({ statusCheckRollup: [{ state: "SUCCESS" }] }), true);
  assert.equal(rollupOk({ statusCheckRollup: [{ state: "FAILURE" }] }), false);
});

test("hasMergeConflict reads CONFLICTING or DIRTY", () => {
  assert.equal(hasMergeConflict({ mergeable: "CONFLICTING", mergeStateStatus: "CLEAN" }), true);
  assert.equal(hasMergeConflict({ mergeable: "MERGEABLE", mergeStateStatus: "DIRTY" }), true);
  assert.equal(hasMergeConflict({ mergeable: "MERGEABLE", mergeStateStatus: "CLEAN" }), false);
});

test("submittedReviewShas skips PENDING drafts", () => {
  assert.deepEqual(
    submittedReviewShas([
      { state: "PENDING", commit: { oid: "draft" } },
      { state: "COMMENTED", commit: { oid: "real" } },
      { state: "APPROVED", commit: null },
    ]),
    ["real"],
  );
});

const HEAD = "head-sha";

function review(login: string, state: string, oid: string, body = ""): ReviewNode {
  return { state, body, author: { login }, commit: { oid } };
}

test("claudeBodyVerdict reads an unconditional approval", () => {
  assert.equal(claudeBodyVerdict("No blocking issues. A docs note: fix the typo."), "APPROVED");
  assert.equal(claudeBodyVerdict("LGTM"), "APPROVED");
});

test("claudeBodyVerdict treats findings, conditions, and blocking words as changes requested", () => {
  assert.equal(claudeBodyVerdict("No blocking issues once the flaky test is fixed."), "CHANGES_REQUESTED");
  assert.equal(claudeBodyVerdict("Changes requested (not approving): 6 open threads."), "CHANGES_REQUESTED");
  assert.equal(claudeBodyVerdict("**Blocking:** the lock drops early."), "CHANGES_REQUESTED");
  assert.equal(claudeBodyVerdict("One blocking issue in spawn.ts."), "CHANGES_REQUESTED");
  assert.equal(claudeBodyVerdict("Two findings in gh.ts."), "CHANGES_REQUESTED");
});

test("claudeBodyVerdict ignores verdict words in quotes and code", () => {
  assert.equal(claudeBodyVerdict("> LGTM\n\nThe loop never exits."), "CHANGES_REQUESTED");
  assert.equal(claudeBodyVerdict("`No blocking issues` is the wrong string."), "CHANGES_REQUESTED");
  assert.equal(claudeBodyVerdict("**Non-blocking:** rename.\n\nNo blocking issues."), "APPROVED");
});

test("headVerdict is approved only when Cursor and Claude both approve the head", () => {
  const cursorOk = review("cursor", "APPROVED", HEAD);
  const claudeOk = review("evandelacruz", "COMMENTED", HEAD, "No blocking issues.");
  assert.equal(headVerdict([cursorOk, claudeOk], HEAD), "APPROVED");
  assert.equal(headVerdict([cursorOk], HEAD), null);
  assert.equal(headVerdict([claudeOk], HEAD), null);
});

test("headVerdict ignores verdicts on an older head", () => {
  const reviews = [
    review("cursor[bot]", "CHANGES_REQUESTED", "old"),
    review("evandelacruz", "COMMENTED", "old", "Not approving."),
  ];
  assert.equal(headVerdict(reviews, HEAD), null);
  assert.equal(
    headVerdict(
      [review("cursor", "APPROVED", "old"), review("evandelacruz", "COMMENTED", HEAD, "LGTM")],
      HEAD,
    ),
    null,
  );
});

test("headVerdict: either reviewer at changes requested wins", () => {
  assert.equal(
    headVerdict(
      [
        review("cursor", "CHANGES_REQUESTED", HEAD),
        review("evandelacruz", "COMMENTED", HEAD, "No blocking issues."),
      ],
      HEAD,
    ),
    "CHANGES_REQUESTED",
  );
});

test("headVerdict uses each reviewer's latest verdict and skips Cursor COMMENTED and empty replies", () => {
  const reviews = [
    review("cursor", "CHANGES_REQUESTED", HEAD),
    review("cursor", "APPROVED", HEAD),
    review("cursor", "COMMENTED", HEAD, "nit"),
    review("evandelacruz", "COMMENTED", HEAD, "Not approving."),
    review("evandelacruz", "COMMENTED", HEAD, "No blocking issues."),
    review("evandelacruz", "COMMENTED", HEAD, ""),
    review("evandelacruz", "PENDING", HEAD, "Requesting changes."),
  ];
  assert.equal(headVerdict(reviews, HEAD), "APPROVED");
});

test("headVerdict ignores other reviewers", () => {
  assert.equal(headVerdict([review("someone", "CHANGES_REQUESTED", HEAD)], HEAD), null);
});

test("holdLock releases when body fails before the agent starts", async () => {
  let released = 0;
  await assert.rejects(
    holdLock(
      async () => {
        released++;
      },
      async () => {
        throw new Error("create failed");
      },
    ),
    /create failed/,
  );
  assert.equal(released, 1);
});

test("holdLock keeps the lock when body fails after the agent starts", async () => {
  let released = 0;
  await assert.rejects(
    holdLock(
      async () => {
        released++;
      },
      async (started) => {
        started();
        throw new Error("wait failed");
      },
    ),
    /wait failed/,
  );
  assert.equal(released, 0);
});

test("holdLock keeps the lock on success and surfaces the original error over a failed release", async () => {
  let released = 0;
  assert.equal(
    await holdLock(
      async () => {
        released++;
      },
      async () => "ok",
    ),
    "ok",
  );
  assert.equal(released, 0);
  await assert.rejects(
    holdLock(
      async () => {
        throw new Error("release failed");
      },
      async () => {
        throw new Error("send failed");
      },
    ),
    /send failed/,
  );
});
