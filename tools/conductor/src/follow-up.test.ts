import { test } from "node:test";
import assert from "node:assert/strict";
import { followUpPrompt } from "./follow-up.js";
import { lockNote } from "./lock.js";
import { prOpeningRule } from "./spawn.js";

const pr = "https://github.com/evandelacruz/agentrealm-agents/pull/18";

test("followUpPrompt is the caller's prompt plus the lock note", () => {
  const out = followUpPrompt("Merge main and fix the conflict.", pr);
  assert.equal(out, `Merge main and fix the conflict.${lockNote(pr)}`);
});

test("followUpPrompt does not inject the PR opening rule", () => {
  const out = followUpPrompt("Fix the red CI job.", pr);
  assert.ok(!out.includes(prOpeningRule));
  assert.doesNotMatch(out, /product sentence/);
});
