import { test } from "node:test";
import assert from "node:assert/strict";
import { buildName, prependIds } from "./spawn.js";

test("buildName prefers an explicit name, then IDs, then the default", () => {
  assert.equal(buildName(["M4"], "LLM planner"), "LLM planner");
  assert.equal(buildName(["M4", "M5"], undefined), "Implement M4, M5");
  assert.equal(buildName([], "  "), "agentrealm-agents implementer");
});

test("prependIds leaves the prompt alone when no IDs are given", () => {
  assert.equal(prependIds("Implement the slice.", []), "Implement the slice.");
});

test("prependIds cites IDs and the agentrealm-agents halt rule", () => {
  const out = prependIds("Scope: planner only.", ["M4"]);
  assert.match(out, /^Backlog IDs: M4\n/);
  assert.match(out, /Cite these IDs in commits and the PR body/);
  assert.match(out, /product sentence/);
  assert.match(out, /can do now, and why it matters/);
  assert.match(out, /Leave the mechanism out/);
  assert.match(out, /diff already shows that/);
  assert.match(out, /Read AGENTS.md and the cited milestones in PLAN.md/);
  assert.match(out, /open design question or a server gap/);
  assert.match(out, /Check PLAN.md/);
  assert.match(out, /Scope: planner only\.$/);
});
