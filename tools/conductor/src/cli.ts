#!/usr/bin/env node
import { flagBool, flagString, parseArgs, readPrompt } from "./args.js";
import { DEFAULT_ENV_NAME, DEFAULT_MODEL, POLISH_DONE_LABEL, WORKING_LABEL, requireApiKey } from "./config.js";
import { followUp } from "./follow-up.js";
import { summarizeOpenPrs, triagePrs } from "./gh.js";
import { spawnImplementer } from "./spawn.js";
import { countRunningAgents, listCloudAgents } from "./status.js";

function printHelp(): void {
  console.log(`agentrealm-agents-conductor — spawn and watch agentrealm-agents implementer agents

Usage:
  npm --prefix tools/conductor run spawn -- [options] -- <prompt>
  npm --prefix tools/conductor run follow-up -- --agent <bc-…> --pr <url> -- <prompt>
  npm --prefix tools/conductor run status [-- --limit 20 | -- --running-count]
  npm --prefix tools/conductor run prs

spawn options:
  --ids A8,A50              Backlog IDs from PLAN.md (comma-separated)
  --name "LLM planner"      Agent display name
  --model composer-2.5      Model id (default: ${DEFAULT_MODEL})
  --env ${DEFAULT_ENV_NAME} Named cloud environment (default)
  --repo <url>              Use explicit repo instead of named env
  --ref main                startingRef when using --repo
  --pr <url>                Attach to an existing PR. Claims conductor:working first.
  --no-pr                   Do not auto-create a PR
  --wait                    Block until the run finishes

follow-up options:
  --agent <bc-…>            Required agent id
  --pr <url>                Required. PR this follow-up writes to. Claims conductor:working first.
  --model <id>              Optional model override
  --wait                    Block until the run finishes

spawn --pr and follow-up refuse a PR that already has conductor:working.
They add that label before the agent starts. Do not delete it so the
command will accept. The prompt tells the agent that holds the lock to
remove only that label after the push. A review still running is the
GitHub Actions check, not a label.

status options:
  --limit 20                Agents to list (default 20)
  --running-count           Print only the number of cloud agents still
                            running, across all pages. Exits non-zero if
                            any agent's status cannot be read.

Environment:
  CURSOR_API_KEY            Required for spawn / follow-up / status
  CURSOR_MODEL              Default model override
  CURSOR_MODEL_FAST         "true" to use a Composer model's fast variant (default: off)
`);
}

function parseIds(raw: string | undefined): string[] {
  if (!raw?.trim()) return [];
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function cmdSpawn(argv: string[]): Promise<void> {
  const { flags, positionals } = parseArgs(argv);
  const prompt = await readPrompt(positionals);
  if (!prompt) {
    throw new Error("spawn requires a prompt (args after -- or stdin)");
  }

  const noPr = flagBool(flags, "no-pr", false);
  const name = flagString(flags, "name");
  const model = flagString(flags, "model");
  const envName = flagString(flags, "env");
  const repoUrl = flagString(flags, "repo");
  const startingRef = flagString(flags, "ref");
  const prUrl = flagString(flags, "pr");
  const result = await spawnImplementer({
    prompt,
    ids: parseIds(flagString(flags, "ids")),
    ...(name !== undefined ? { name } : {}),
    ...(model !== undefined ? { model } : {}),
    ...(envName !== undefined ? { envName } : {}),
    ...(repoUrl !== undefined ? { repoUrl } : {}),
    ...(startingRef !== undefined ? { startingRef } : {}),
    ...(prUrl !== undefined ? { prUrl } : {}),
    autoCreatePR: !noPr,
    wait: flagBool(flags, "wait", false),
  });

  console.log(JSON.stringify(result, null, 2));
  console.log(`\nAgent: https://cursor.com/agents/${result.agentId}`);
}

async function cmdFollowUp(argv: string[]): Promise<void> {
  const { flags, positionals } = parseArgs(argv);
  const agentId = flagString(flags, "agent");
  if (!agentId) {
    throw new Error("follow-up requires --agent <bc-…>");
  }
  const prompt = await readPrompt(positionals);
  if (!prompt) {
    throw new Error("follow-up requires a prompt (args after -- or stdin)");
  }

  const prUrl = flagString(flags, "pr");
  if (!prUrl) {
    throw new Error("follow-up requires --pr <url> so conductor:working can be claimed first");
  }
  const model = flagString(flags, "model");
  const result = await followUp({
    agentId,
    prUrl,
    prompt,
    ...(model !== undefined ? { model } : {}),
    wait: flagBool(flags, "wait", false),
  });

  console.log(JSON.stringify(result, null, 2));
  console.log(`\nAgent: https://cursor.com/agents/${result.agentId}`);
}

async function cmdStatus(argv: string[]): Promise<void> {
  const { flags } = parseArgs(argv);
  if (flagBool(flags, "running-count", false)) {
    console.log(await countRunningAgents({ apiKey: requireApiKey() }));
    return;
  }
  const limitRaw = flagString(flags, "limit");
  const limit = limitRaw ? Number(limitRaw) : 20;
  const agents = await listCloudAgents(Number.isFinite(limit) ? limit : 20);
  if (agents.length === 0) {
    console.log("No cloud agents found (SDK list is empty).");
    return;
  }
  for (const a of agents) {
    console.log([a.agentId, a.name, a.status, a.lastModified, a.url].join("\t"));
  }
}

function lockLabel(labels: string[]): string {
  if (labels.includes(WORKING_LABEL)) return "lock:working";
  return "lock:none";
}

function polishLabel(labels: string[]): string {
  return labels.includes(POLISH_DONE_LABEL) ? "polish:done" : "polish:due";
}

async function cmdPrs(): Promise<void> {
  const summaries = await summarizeOpenPrs();
  if (summaries.length === 0) {
    console.log("No open PRs.");
    return;
  }

  for (const s of summaries) {
    const checks =
      s.checksOk === null ? "checks:?" : s.checksOk ? "checks:ok" : "checks:fail";
    const review = s.verdict ?? "review:none";
    const draft = s.isDraft ? "draft" : "ready";
    console.log(
      [
        `#${s.number}`,
        draft,
        review,
        lockLabel(s.labels),
        polishLabel(s.labels),
        checks,
        s.reviewInProgress ? "review-check:running" : "review-check:idle",
        s.hasMergeConflict ? "merge:conflict" : "merge:ok",
        `unresolved:${s.unresolvedReviewThreads}`,
        `comments:${s.issueComments}`,
        s.headRefName,
        s.title,
        s.url,
      ].join("\t"),
    );
  }

  const { needsFix, awaitingPolish, mergeReady } = triagePrs(summaries);
  if (needsFix.length > 0) {
    console.log("\nNeeds fixer follow-up:");
    for (const s of needsFix) {
      console.log(`  #${s.number} ${s.title} ${s.url}`);
    }
  }

  // Every approved PR gets one polish pass from a Claude fixer, which ends by
  // adding polish-done. The conductor never polishes; it only lists them.
  if (awaitingPolish.length > 0) {
    console.log("\nApproved, awaiting the Claude polish pass (not a Cursor follow-up):");
    for (const s of awaitingPolish) {
      console.log(`  #${s.number} (${s.unresolvedReviewThreads} open) ${s.url}`);
    }
  }

  if (mergeReady.length > 0) {
    console.log("\nReady to merge: approved, green, polished (conductor never merges):");
    for (const s of mergeReady) {
      console.log(`  #${s.number} ${s.title} ${s.url}`);
    }
  }
}

async function main(): Promise<void> {
  const [command, ...rest] = process.argv.slice(2);
  if (!command || command === "-h" || command === "--help" || command === "help") {
    printHelp();
    return;
  }

  switch (command) {
    case "spawn":
      await cmdSpawn(rest);
      break;
    case "follow-up":
      await cmdFollowUp(rest);
      break;
    case "status":
      await cmdStatus(rest);
      break;
    case "prs":
      await cmdPrs();
      break;
    default:
      throw new Error(`Unknown command: ${command}\n\nRun with --help for usage.`);
  }
}

main().catch((err) => {
  const message = err instanceof Error ? err.message : String(err);
  console.error(message);
  process.exitCode = 1;
});
