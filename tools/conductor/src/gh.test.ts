import { test } from "node:test";
import assert from "node:assert/strict";
import { APPROVED_LABEL, CHANGES_REQUESTED_LABEL, REVIEW_CHECK_NAME } from "./config.js";
import {
  effectiveReviewDecision,
  hasMergeConflict,
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

test("effectiveReviewDecision prefers a real GitHub decision", () => {
  assert.equal(
    effectiveReviewDecision({
      reviewDecision: "CHANGES_REQUESTED",
      labels: [APPROVED_LABEL],
    }),
    "CHANGES_REQUESTED",
  );
});

test("effectiveReviewDecision falls back to verdict labels", () => {
  assert.equal(
    effectiveReviewDecision({ reviewDecision: null, labels: [APPROVED_LABEL] }),
    "APPROVED",
  );
  assert.equal(
    effectiveReviewDecision({
      reviewDecision: null,
      labels: [APPROVED_LABEL, CHANGES_REQUESTED_LABEL],
    }),
    "CHANGES_REQUESTED",
  );
  assert.equal(effectiveReviewDecision({ reviewDecision: null, labels: [] }), null);
});
