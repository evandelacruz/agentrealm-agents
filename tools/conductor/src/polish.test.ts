import { test } from "node:test";
import assert from "node:assert/strict";
import { POLISH_DONE_LABEL, WORKING_LABEL } from "./config.js";
import { awaitingPolish, mergeReady, type PolishFields } from "./polish.js";

function pr(over: Partial<PolishFields> = {}): PolishFields {
  return {
    isDraft: false,
    verdict: "APPROVED",
    hasMergeConflict: false,
    checksOk: true,
    reviewInProgress: false,
    labels: [],
    ...over,
  };
}

// Matches the fixer fleet's polish row: one pass per approved PR (agentrealm-agents-fixer Polish).
test("an approved, green, unpolished PR awaits polish and is not merge-ready", () => {
  assert.equal(awaitingPolish(pr()), true);
  assert.equal(mergeReady(pr()), false);
});

test("polish-done moves an approved, green PR to merge-ready", () => {
  const s = pr({ labels: [POLISH_DONE_LABEL] });
  assert.equal(awaitingPolish(s), false);
  assert.equal(mergeReady(s), true);
});

// A writer holds the lock: the PR is neither the fleet's nor ready to merge.
test("conductor:working keeps a PR off both lists", () => {
  assert.equal(awaitingPolish(pr({ labels: [WORKING_LABEL] })), false);
  assert.equal(mergeReady(pr({ labels: [WORKING_LABEL, POLISH_DONE_LABEL] })), false);
});

// A review in flight holds every PR back; red CI and conflicts go to fixers, not polish.
test("review in flight, red CI, conflict, draft, or no approval block both lists", () => {
  for (const over of [
    { reviewInProgress: true },
    { checksOk: false },
    { hasMergeConflict: true },
    { isDraft: true },
    { verdict: null },
    { verdict: "CHANGES_REQUESTED" as const },
  ]) {
    assert.equal(awaitingPolish(pr(over)), false, JSON.stringify(over));
    assert.equal(mergeReady(pr({ ...over, labels: [POLISH_DONE_LABEL] })), false, JSON.stringify(over));
  }
});

test("checks still running do not block polish but do block merge", () => {
  assert.equal(awaitingPolish(pr({ checksOk: null })), true);
  assert.equal(mergeReady(pr({ checksOk: null, labels: [POLISH_DONE_LABEL] })), false);
});
