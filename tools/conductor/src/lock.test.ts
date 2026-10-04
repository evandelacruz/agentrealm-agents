import { test } from "node:test";
import assert from "node:assert/strict";
import { WORKING_LABEL } from "./config.js";
import { lockHeld, lockNote, pullRequestNumber } from "./lock.js";

test("lockHeld is conductor:working only", () => {
  assert.equal(lockHeld([]), false);
  assert.equal(lockHeld(["bug", "conductor:reviewing"]), false);
  assert.equal(lockHeld([WORKING_LABEL]), true);
});

test("pullRequestNumber accepts a number or a pull URL", () => {
  assert.equal(pullRequestNumber("18"), 18);
  assert.equal(pullRequestNumber("#18"), 18);
  assert.equal(pullRequestNumber("https://github.com/evandelacruz/agentrealm-agents/pull/18"), 18);
  assert.equal(pullRequestNumber("https://github.com/evandelacruz/agentrealm-agents/pull/18/"), 18);
  assert.throws(() => pullRequestNumber("https://github.com/evandelacruz/agentrealm-agents/issues/18"), /Not a pull request/);
});

test("lockNote for an existing PR tells the agent to drop only conductor:working", () => {
  const note = lockNote("https://github.com/evandelacruz/agentrealm-agents/pull/18");
  assert.match(note, /PR #18 is labeled conductor:working/);
  assert.match(note, /gh pr edit 18 --remove-label conductor:working/);
  assert.match(note, /Do not replace the label set/);
  assert.match(note, /Do not remove conductor:working from any other pull request/);
});

test("lockNote for new work tells the agent to claim the label when the PR exists", () => {
  const note = lockNote(undefined);
  assert.match(note, /add conductor:working/);
  assert.match(note, /stop without pushing/);
  assert.match(note, /Never remove conductor:working from a pull request you did not just open/);
  assert.doesNotMatch(note, /conductor:reviewing/);
});
