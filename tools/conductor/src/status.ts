import { Agent } from "@cursor/sdk";
import { requireApiKey } from "./config.js";

export type CloudAgentSummary = {
  agentId: string;
  name: string;
  status: string;
  createdAt: string | undefined;
  lastModified: string;
  url: string;
};

/** Minimal listRuns shape used for status hydration (injectable in tests). */
export type ListRunsFn = (
  agentId: string,
  opts: { runtime: "cloud"; apiKey: string; limit: number },
) => Promise<{ items: Array<{ status?: string }> }>;

/**
 * Status of an agent's latest run: the status `Agent.list` returned when
 * present, otherwise its latest run's (`Agent.listRuns`, paced by `pace`).
 * Throws when the lookup fails or the run has no status. Every per-agent
 * `listRuns` call in this file goes through here — see
 * `countRunningAgents` on the rate limit.
 */
export async function fetchAgentStatus(options: {
  agentId: string;
  listedStatus: string | undefined;
  apiKey: string;
  /** Override for tests — defaults to `Agent.listRuns`. */
  listRuns?: ListRunsFn;
  /** Waits for a `listRuns` slot — defaults to none. */
  pace?: () => Promise<void>;
}): Promise<string> {
  if (options.listedStatus !== undefined && options.listedStatus.length > 0) {
    return options.listedStatus;
  }
  const listRuns = options.listRuns ?? Agent.listRuns;
  await options.pace?.();
  const { items } = await listRuns(options.agentId, {
    runtime: "cloud",
    apiKey: options.apiKey,
    limit: 1,
  });
  const status = items[0]?.status;
  if (!status) {
    throw new Error(`agent ${options.agentId}: latest run has no status`);
  }
  return status;
}

/**
 * Cloud `Agent.list` often omits `status` even though the type allows it.
 * When missing, resolve from the agent's latest run (`Agent.listRuns`).
 * Without it, every finished agent looks the same as a running one (`unknown`)
 * and a later pass re-spawns fixers on PRs that already have an agent.
 */
export async function resolveAgentStatus(options: {
  agentId: string;
  listedStatus: string | undefined;
  apiKey: string;
  /** Override for tests — defaults to `Agent.listRuns`. */
  listRuns?: ListRunsFn;
  pace?: () => Promise<void>;
}): Promise<string> {
  try {
    return await fetchAgentStatus(options);
  } catch {
    // Caller treats unknown as not-in-flight.
    return "unknown";
  }
}

/** Minimal Agent.list shape (injectable in tests). */
export type ListAgentsFn<
  T extends { agentId: string; status?: string } = { agentId: string; status?: string },
> = (opts: {
  runtime: "cloud";
  apiKey: string;
  limit: number;
  cursor?: string;
}) => Promise<{ items: T[]; nextCursor?: string }>;

/** The `Agent.list` fields `listCloudAgents` reports. */
export type ListedAgent = {
  agentId: string;
  name: string;
  status?: string;
  createdAt?: number;
  lastModified: number;
};

/**
 * Most `listRuns` calls `countRunningAgents` may have in flight at once.
 * Cursor caps `list_agent_runs` at 300 requests per minute; an unbounded
 * `Promise.all` over a 100-agent page hit that limit against the live API.
 */
export const LIST_RUNS_CONCURRENCY = 4;

/**
 * Minimum gap between `listRuns` starts in `countRunningAgents`. The
 * concurrency cap alone bounds calls in flight, not calls per minute: fast
 * responses let 4 workers exceed 300/min. 250 ms is at most 240/min, leaving
 * headroom under the 300/min limit for other callers sharing the key.
 */
export const LIST_RUNS_MIN_INTERVAL_MS = 250;

/**
 * Returns a function that resolves no sooner than `intervalMs` after the
 * previous call's slot, so callers start at most one per interval however
 * many are waiting. `now` and `sleep` are injectable for tests.
 */
export function createPacer(
  intervalMs: number,
  now: () => number = Date.now,
  sleep: (ms: number) => Promise<void> = (ms) =>
    new Promise((r) => setTimeout(r, ms)),
): () => Promise<void> {
  let nextSlot = -Infinity;
  return async () => {
    const t = now();
    const slot = Math.max(t, nextSlot);
    nextSlot = slot + intervalMs;
    if (slot > t) await sleep(slot - t);
  };
}

/**
 * Like `Promise.all(items.map(fn))`, with at most `limit` calls pending.
 * After the first rejection no further calls start.
 */
export async function mapWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  let failed = false;
  const worker = async () => {
    while (!failed && next < items.length) {
      const i = next++;
      try {
        results[i] = await fn(items[i]!);
      } catch (err) {
        failed = true;
        throw err;
      }
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, worker),
  );
  return results;
}

/**
 * Counts cloud agents whose latest run is still running, across every page
 * of `Agent.list`. The SDK maps a cloud run's CREATING and RUNNING to
 * "running"; finished, error, and cancelled are the only other values, so
 * "running" is exactly "still in flight".
 *
 * The supervisor subtracts this from its open-PR budget: an implementer it
 * spawned on an earlier pass has no pull request until it opens one, and
 * without this count the next pass spawns the same slots again. Unlike
 * `resolveAgentStatus`, any failure throws — a count that silently drops an
 * agent reopens that double spawn.
 *
 * Rate limit: `list_agent_runs` allows 300 requests per minute. Use the status
 * `Agent.list` already returns and call `listRuns` only when it is missing,
 * at most `LIST_RUNS_CONCURRENCY` at a time and no more than one start per
 * `LIST_RUNS_MIN_INTERVAL_MS`. Do not fan out one call per agent, and do not
 * drop the pacing in favour of the concurrency cap alone — either can trip
 * the limit once agent history grows.
 */
export async function countRunningAgents(options: {
  apiKey: string;
  /** Overrides for tests — default to `Agent.list` / `Agent.listRuns`. */
  listAgents?: ListAgentsFn;
  listRuns?: ListRunsFn;
  pageSize?: number;
  concurrency?: number;
  /** Paces `listRuns` starts — defaults to `LIST_RUNS_MIN_INTERVAL_MS`. */
  pace?: () => Promise<void>;
}): Promise<number> {
  const listAgents = options.listAgents ?? Agent.list;
  const listRuns = options.listRuns ?? Agent.listRuns;
  const pageSize = options.pageSize ?? 100;
  const concurrency = options.concurrency ?? LIST_RUNS_CONCURRENCY;
  const pace = options.pace ?? createPacer(LIST_RUNS_MIN_INTERVAL_MS);

  let running = 0;
  let cursor: string | undefined;
  do {
    const page = await listAgents({
      runtime: "cloud",
      apiKey: options.apiKey,
      limit: pageSize,
      ...(cursor !== undefined ? { cursor } : {}),
    });
    const statuses = await mapWithConcurrency(page.items, concurrency, (a) =>
      fetchAgentStatus({
        agentId: a.agentId,
        listedStatus: a.status,
        apiKey: options.apiKey,
        listRuns,
        pace,
      }),
    );
    running += statuses.filter((s) => s === "running").length;
    cursor = page.nextCursor;
  } while (cursor);
  return running;
}

/**
 * Lists recent cloud agents with their status. Hydrates missing statuses
 * under the same concurrency cap and pacer as `countRunningAgents`, so a
 * large `--limit` cannot trip the `list_agent_runs` rate limit either.
 */
export async function listCloudAgents(
  limit = 20,
  options: {
    /** Overrides for tests — default to `Agent.list` / `Agent.listRuns`. */
    listAgents?: ListAgentsFn<ListedAgent>;
    listRuns?: ListRunsFn;
    pace?: () => Promise<void>;
    apiKey?: string;
  } = {},
): Promise<CloudAgentSummary[]> {
  const apiKey = options.apiKey ?? requireApiKey();
  const listAgents = options.listAgents ?? Agent.list;
  const pace = options.pace ?? createPacer(LIST_RUNS_MIN_INTERVAL_MS);
  const { items } = await listAgents({
    runtime: "cloud",
    apiKey,
    limit,
  });

  return mapWithConcurrency(items, LIST_RUNS_CONCURRENCY, async (a) => {
    const status = await resolveAgentStatus({
      agentId: a.agentId,
      listedStatus: a.status,
      apiKey,
      ...(options.listRuns !== undefined ? { listRuns: options.listRuns } : {}),
      pace,
    });
    return {
      agentId: a.agentId,
      name: a.name,
      status,
      createdAt:
        a.createdAt !== undefined ? new Date(a.createdAt).toISOString() : undefined,
      lastModified: new Date(a.lastModified).toISOString(),
      url: `https://cursor.com/agents/${a.agentId}`,
    };
  });
}
