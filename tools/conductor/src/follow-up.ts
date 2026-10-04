import { Agent } from "@cursor/sdk";
import { DEFAULT_MODEL, requireApiKey } from "./config.js";
import { withWorkingLock } from "./gh.js";
import { lockNote } from "./lock.js";

export type FollowUpOptions = {
  agentId: string;
  /** Pull request this follow-up writes to. Required so the working lock is claimed first. */
  prUrl: string;
  prompt: string;
  model?: string;
  wait?: boolean;
};

/**
 * The follow-up prompt is the caller's text plus the lock note. It adds no
 * PR-description rule: most follow-ups (conflicts, red CI) never touch the
 * body, and the follow-up brief already says "If you edit the PR description…".
 */
export function followUpPrompt(prompt: string, prUrl: string): string {
  return `${prompt}${lockNote(prUrl)}`;
}

export async function followUp(options: FollowUpOptions): Promise<{
  agentId: string;
  runId: string;
  status: string;
}> {
  const apiKey = requireApiKey();
  const prompt = followUpPrompt(options.prompt, options.prUrl);

  return withWorkingLock(options.prUrl, async (started) => {
    const agent = await Agent.resume(options.agentId, {
      apiKey,
      ...(options.model ? { model: { id: options.model } } : { model: { id: DEFAULT_MODEL } }),
    });

    const run = await agent.send(prompt);
    // The agent is writing now. From here on a failure must not drop the lock.
    started();

    if (options.wait) {
      const result = await run.wait();
      return {
        agentId: agent.agentId,
        runId: run.id,
        status: result.status,
      };
    }

    return {
      agentId: agent.agentId,
      runId: run.id,
      status: "started",
    };
  });
}
