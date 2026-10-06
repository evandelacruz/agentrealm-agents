import { test } from "node:test";
import assert from "node:assert/strict";
import { modelSelection } from "./config.js";

test("composer models are pinned to their non-fast variant", () => {
  assert.deepEqual(modelSelection("composer-2.5"), {
    id: "composer-2.5",
    params: [{ id: "fast", value: "false" }],
  });
});

test("other models get no fast parameter", () => {
  assert.deepEqual(modelSelection("gpt-5"), { id: "gpt-5" });
});
