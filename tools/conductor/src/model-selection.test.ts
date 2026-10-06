import { test } from "node:test";
import assert from "node:assert/strict";
import { modelSelection } from "./config.js";

test("composer models are pinned to their non-fast variant", () => {
  assert.deepEqual(modelSelection("composer-2.5", {}), {
    id: "composer-2.5",
    params: [{ id: "fast", value: "false" }],
  });
});

test("CURSOR_MODEL_FAST=true opts back in to fast", () => {
  assert.deepEqual(modelSelection("composer-2.5", { CURSOR_MODEL_FAST: "true" }), {
    id: "composer-2.5",
    params: [{ id: "fast", value: "true" }],
  });
});

test("other models get no fast parameter", () => {
  assert.deepEqual(modelSelection("gpt-5", {}), { id: "gpt-5" });
});
