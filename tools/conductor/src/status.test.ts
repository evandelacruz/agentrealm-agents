import { test } from "node:test";
import assert from "node:assert/strict";
import {
  countRunningAgents,
  createPacer,
  fetchAgentStatus,
  listCloudAgents,
  LIST_RUNS_CONCURRENCY,
  LIST_RUNS_MIN_INTERVAL_MS,
  mapWithConcurrency,
  resolveAgentStatus,
  type ListAgentsFn,
  type ListRunsFn,
  type ListedAgent,
} from "./status.js";

test("resolveAgentStatus uses listed status when present", async () => {
  let listRunsCalled = false;
  const listRuns: ListRunsFn = async () => {
    listRunsCalled = true;
    return { items: [{ status: "running" }] };
  };
  const status = await resolveAgentStatus({
    agentId: "bc-test",
    listedStatus: "finished",
    apiKey: "test-key",
    listRuns,
  });
  assert.equal(status, "finished");
  assert.equal(listRunsCalled, false);
});

test("resolveAgentStatus hydrates from latest run when listed status missing", async () => {
  const listRuns: ListRunsFn = async (agentId, opts) => {
    assert.equal(agentId, "bc-hydrate");
    assert.equal(opts.limit, 1);
    assert.equal(opts.runtime, "cloud");
    return { items: [{ status: "waiting_for_background_work" }] };
  };
  const status = await resolveAgentStatus({
    agentId: "bc-hydrate",
    listedStatus: undefined,
    apiKey: "test-key",
    listRuns,
  });
  assert.equal(status, "waiting_for_background_work");
});

test("resolveAgentStatus treats empty listed status as missing", async () => {
  const listRuns: ListRunsFn = async () => ({
    items: [{ status: "running" }],
  });
  const status = await resolveAgentStatus({
    agentId: "bc-empty",
    listedStatus: "",
    apiKey: "test-key",
    listRuns,
  });
  assert.equal(status, "running");
});

test("resolveAgentStatus returns unknown when listRuns throws", async () => {
  const listRuns: ListRunsFn = async () => {
    throw new Error("network");
  };
  const status = await resolveAgentStatus({
    agentId: "bc-err",
    listedStatus: undefined,
    apiKey: "test-key",
    listRuns,
  });
  assert.equal(status, "unknown");
});

test("resolveAgentStatus returns unknown when latest run has no status", async () => {
  const listRuns: ListRunsFn = async () => ({ items: [{}] });
  const status = await resolveAgentStatus({
    agentId: "bc-nostatus",
    listedStatus: undefined,
    apiKey: "test-key",
    listRuns,
  });
  assert.equal(status, "unknown");
});

const noPace = async () => {};

// Protects the supervisor's in-flight guard: an implementer with no pull
// request yet must still count, on every page, or the next pass double-spawns.
test("countRunningAgents counts running latest runs across every page", async () => {
  const pages: Record<string, { items: Array<{ agentId: string }>; nextCursor?: string }> = {
    "": { items: [{ agentId: "bc-a" }, { agentId: "bc-b" }], nextCursor: "p2" },
    p2: { items: [{ agentId: "bc-c" }, { agentId: "bc-d" }] },
  };
  const listAgents: ListAgentsFn = async (opts) => pages[opts.cursor ?? ""]!;
  const statuses: Record<string, string> = {
    "bc-a": "running",
    "bc-b": "finished",
    "bc-c": "running",
    "bc-d": "cancelled",
  };
  const listRuns: ListRunsFn = async (agentId) => ({
    items: [{ status: statuses[agentId]! }],
  });
  const n = await countRunningAgents({
    apiKey: "k",
    listAgents,
    listRuns,
    pageSize: 2,
    pace: noPace,
  });
  assert.equal(n, 2);
});

test("countRunningAgents throws when a status cannot be read", async () => {
  const listAgents: ListAgentsFn = async () => ({ items: [{ agentId: "bc-x" }] });
  const cases: ListRunsFn[] = [
    async () => ({ items: [] }),
    async () => {
      throw new Error("network");
    },
  ];
  for (const listRuns of cases) {
    await assert.rejects(countRunningAgents({ apiKey: "k", listAgents, listRuns, pace: noPace }));
  }
});

// Regression: one listRuns per agent via Promise.all over a 100-agent page hit
// "exceeded the rate limit of 300 requests per minute for the list_agent_runs
// endpoint", and the supervisor fails closed on that, so dispatch stopped.
test("countRunningAgents uses listed status and calls listRuns only when missing", async () => {
  const listAgents: ListAgentsFn = async () => ({
    items: [
      { agentId: "bc-a", status: "running" },
      { agentId: "bc-b", status: "finished" },
      { agentId: "bc-c", status: "error" },
      { agentId: "bc-d" },
      { agentId: "bc-e", status: "" },
    ],
  });
  const called: string[] = [];
  const listRuns: ListRunsFn = async (agentId) => {
    called.push(agentId);
    return { items: [{ status: agentId === "bc-d" ? "running" : "finished" }] };
  };
  const n = await countRunningAgents({ apiKey: "k", listAgents, listRuns, pace: noPace });
  assert.equal(n, 2);
  assert.deepEqual(called.sort(), ["bc-d", "bc-e"]);
});

test("countRunningAgents keeps at most LIST_RUNS_CONCURRENCY listRuns calls in flight", async () => {
  const items = Array.from({ length: 100 }, (_, i) => ({ agentId: `bc-${i}` }));
  const listAgents: ListAgentsFn = async () => ({ items });
  let inFlight = 0;
  let peak = 0;
  let calls = 0;
  const listRuns: ListRunsFn = async () => {
    calls++;
    inFlight++;
    peak = Math.max(peak, inFlight);
    await new Promise((r) => setImmediate(r));
    inFlight--;
    return { items: [{ status: "finished" }] };
  };
  const n = await countRunningAgents({ apiKey: "k", listAgents, listRuns, pace: noPace });
  assert.equal(n, 0);
  assert.equal(calls, 100);
  assert.equal(peak, LIST_RUNS_CONCURRENCY);
});

test("countRunningAgents fails closed on a rate-limit error and stops calling", async () => {
  const items = Array.from({ length: 50 }, (_, i) => ({ agentId: `bc-${i}` }));
  const listAgents: ListAgentsFn = async () => ({ items });
  let calls = 0;
  const listRuns: ListRunsFn = async () => {
    calls++;
    await new Promise((r) => setImmediate(r));
    throw new Error(
      "exceeded the rate limit of 300 requests per minute for the list_agent_runs endpoint",
    );
  };
  await assert.rejects(
    countRunningAgents({ apiKey: "k", listAgents, listRuns, pace: noPace }),
    /rate limit/,
  );
  assert.ok(calls <= LIST_RUNS_CONCURRENCY, `made ${calls} calls after the first failure`);
});

test("mapWithConcurrency preserves order", async () => {
  const out = await mapWithConcurrency([3, 1, 2], 2, async (x) => {
    await new Promise((r) => setTimeout(r, x));
    return x * 10;
  });
  assert.deepEqual(out, [30, 10, 20]);
});

// The concurrency cap bounds calls in flight, not calls per minute: with fast
// responses 4 workers alone exceed list_agent_runs' 300/min. Every listRuns
// start must go through the pacer.
test("countRunningAgents paces every listRuns start", async () => {
  const items = Array.from({ length: 10 }, (_, i) => ({ agentId: `bc-${i}` }));
  const listAgents: ListAgentsFn = async () => ({
    items: [...items, { agentId: "bc-listed", status: "running" }],
  });
  let paced = 0;
  let calls = 0;
  const pace = async () => {
    paced++;
  };
  const listRuns: ListRunsFn = async () => {
    calls++;
    assert.ok(calls <= paced, "listRuns started without a pacer slot");
    return { items: [{ status: "finished" }] };
  };
  await countRunningAgents({ apiKey: "k", listAgents, listRuns, pace });
  assert.equal(paced, 10);
  assert.equal(calls, 10);
});

test("createPacer holds starts to one per interval, at most 300 per minute", async () => {
  assert.ok(60_000 / LIST_RUNS_MIN_INTERVAL_MS < 300);
  let clock = 0;
  const sleeps: number[] = [];
  const pace = createPacer(
    LIST_RUNS_MIN_INTERVAL_MS,
    () => clock,
    async (ms) => {
      sleeps.push(ms);
    },
  );
  // Five callers arrive at once: first goes now, the rest queue one interval apart.
  await Promise.all([pace(), pace(), pace(), pace(), pace()]);
  assert.deepEqual(sleeps, [250, 500, 750, 1000]);
  // After an idle gap longer than the backlog, the next caller goes immediately.
  clock = 5_000;
  sleeps.length = 0;
  await pace();
  assert.deepEqual(sleeps, []);
});

test("fetchAgentStatus throws where resolveAgentStatus returns unknown", async () => {
  const cases: ListRunsFn[] = [
    async () => ({ items: [] }),
    async () => {
      throw new Error("network");
    },
  ];
  for (const listRuns of cases) {
    const opts = { agentId: "bc-x", listedStatus: undefined, apiKey: "k", listRuns };
    await assert.rejects(fetchAgentStatus(opts));
    assert.equal(await resolveAgentStatus(opts), "unknown");
  }
});

// Plain `status -- --limit 200` hydrates the same way as --running-count, so
// it must not fan out unpaced either (list_agent_runs: 300 requests/minute).
test("listCloudAgents paces and caps listRuns, using listed status first", async () => {
  const items: ListedAgent[] = Array.from({ length: 40 }, (_, i) => ({
    agentId: `bc-${i}`,
    name: `agent ${i}`,
    lastModified: 0,
    ...(i % 4 === 0 ? { status: "finished" } : {}),
  }));
  const listAgents: ListAgentsFn<ListedAgent> = async (opts) => {
    assert.equal(opts.limit, 200);
    return { items };
  };
  let paced = 0;
  let calls = 0;
  let inFlight = 0;
  let peak = 0;
  const pace = async () => {
    paced++;
  };
  const listRuns: ListRunsFn = async () => {
    calls++;
    assert.ok(calls <= paced, "listRuns started without a pacer slot");
    inFlight++;
    peak = Math.max(peak, inFlight);
    await new Promise((r) => setImmediate(r));
    inFlight--;
    return { items: [{ status: "running" }] };
  };
  const agents = await listCloudAgents(200, { apiKey: "k", listAgents, listRuns, pace });
  assert.equal(agents.length, 40);
  assert.equal(calls, 30);
  assert.equal(paced, 30);
  assert.ok(peak <= LIST_RUNS_CONCURRENCY);
  assert.equal(agents[0]!.status, "finished");
  assert.equal(agents[1]!.status, "running");
  assert.equal(agents[1]!.url, "https://cursor.com/agents/bc-1");
});
