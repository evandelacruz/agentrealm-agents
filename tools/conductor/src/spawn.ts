import { Agent } from "@cursor/sdk";
import {
  DEFAULT_ENV_NAME,
  DEFAULT_MODEL,
  DEFAULT_REPO_URL,
  DEFAULT_STARTING_REF,
  requireApiKey,
} from "./config.js";
import { withWorkingLock } from "./gh.js";
import { lockNote } from "./lock.js";

export type SpawnOptions = {
  prompt: string;
  ids: string[];
  name?: string;
  model?: string;
  /** Named Cursor cloud environment (default: evandelacruz/agentrealm-agents). */
  envName?: string;
  /** If set, use explicit repos instead of a named environment. */
  repoUrl?: string;
  startingRef?: string;
  autoCreatePR?: boolean;
  /** Attach to an existing PR (uses repos + prUrl; ignores named env). */
  prUrl?: string;
  wait?: boolean;
};

export function buildName(ids: string[], name: string | undefined): string {
  if (name?.trim()) return name.trim();
  if (ids.length > 0) return `Implement ${ids.join(", ")}`;
  return "agentrealm-agents implementer";
}

// prOpeningRule is the first text of every pull request body. It is product
// language. The diff already carries the mechanism.
export const prOpeningRule =
  "The PR description opens with a product sentence. The first text is one or two sentences in plain language: what someone running a reference agent, or the agent itself, can do now, and why it matters. Write it the way a product person would. Leave the mechanism out — no functions, files, types, or a walk through the logic — because the diff already shows that. Backlog IDs and the rest of the detail come after that sentence. For example: \"A scripted character now walks back to the chest it dropped when it died.\"";

export function prependIds(prompt: string, ids: string[]): string {
  if (ids.length === 0) return prompt;
  const header = [
    `Backlog IDs: ${ids.join(", ")}`,
    "Cite these IDs in commits and the PR body.",
    prOpeningRule,
    "Read AGENTS.md and the cited backlog items in PLAN.md before coding.",
    "If blocked by an open architecture, legal, or moderation question, an open design question, or a server gap — halt and print why. Do not invent. Check PLAN.md and the published Agent Realm docs first.",
    "",
  ].join("\n");
  return `${header}${prompt}`;
}

export async function spawnImplementer(options: SpawnOptions): Promise<{
  agentId: string;
  name: string;
  runId?: string;
  status?: string;
}> {
  const apiKey = requireApiKey();
  const modelId = options.model ?? DEFAULT_MODEL;
  const name = buildName(options.ids, options.name);
  const prompt = `${prependIds(options.prompt, options.ids)}${lockNote(options.prUrl)}`;
  const autoCreatePR = options.autoCreatePR ?? true;

  // Named Cursor environments already bind the repo; repos + env.name are mutually exclusive.
  const useRepo = options.prUrl !== undefined || options.repoUrl !== undefined;

  const cloud = useRepo
    ? {
        repos: [
          {
            url: options.repoUrl ?? DEFAULT_REPO_URL,
            startingRef: options.startingRef ?? DEFAULT_STARTING_REF,
            ...(options.prUrl ? { prUrl: options.prUrl } : {}),
          },
        ],
        autoCreatePR: options.prUrl ? false : autoCreatePR,
      }
    : {
        env: { type: "cloud" as const, name: options.envName ?? DEFAULT_ENV_NAME },
        autoCreatePR,
      };

  const start = async (started: () => void = () => {}) => {
    const agent = await Agent.create({
      apiKey,
      name,
      model: { id: modelId },
      cloud,
    });

    const run = await agent.send(prompt);
    // The agent is writing now. From here on a failure must not drop the lock.
    started();

    if (options.wait) {
      const result = await run.wait();
      try {
        agent.close();
      } catch {
        // Best-effort; cloud run already completed.
      }
      return {
        agentId: agent.agentId,
        name,
        runId: run.id,
        status: result.status,
      };
    }

    // Detach: drop local SDK handles so the Node process can exit; cloud continues.
    try {
      agent.close();
    } catch {
      // Best-effort; spawn already succeeded.
    }

    return {
      agentId: agent.agentId,
      name,
      runId: run.id,
      status: "started",
    };
  };

  // An existing PR is claimed before the agent starts. New work has no PR yet;
  // the prompt tells that agent to add the label once the PR exists.
  if (options.prUrl) return withWorkingLock(options.prUrl, start);
  return start();
}
