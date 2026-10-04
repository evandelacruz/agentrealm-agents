import { test } from "node:test";
import assert from "node:assert/strict";
import { flagBool, flagString, parseArgs } from "./args.js";

test("parseArgs collects long flags, equals-form, and -- rest", () => {
  const { flags, positionals } = parseArgs([
    "--ids",
    "M4,M5",
    "--no-pr",
    "--name=LLM planner",
    "--",
    "Implement",
    "M4",
  ]);
  assert.equal(flagString(flags, "ids"), "M4,M5");
  assert.equal(flagBool(flags, "no-pr", false), true);
  assert.equal(flagString(flags, "name"), "LLM planner");
  assert.deepEqual(positionals, ["Implement", "M4"]);
});

test("flagBool treats missing as the default and false/0 as false", () => {
  const { flags } = parseArgs(["--wait=false", "--verbose", "0"]);
  assert.equal(flagBool(flags, "wait", true), false);
  assert.equal(flagBool(flags, "missing", true), true);
  assert.equal(flagString(flags, "verbose"), "0");
});
