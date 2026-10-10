import { test } from "node:test";
import assert from "node:assert/strict";
import { CLAUDE_REVIEW_WORKFLOW, REVIEW_CHECK_NAME } from "./config.js";
import {
  hasMergeConflict,
  headVerdict,
  holdLock,
  parseReviewers,
  trustedReview,
  type ReviewNode,
  reviewInProgress,
  rollupOk,
  submittedReviewShas,
  triagePrs,
} from "./gh.js";

test("reviewInProgress is a reviewer check while it is running", () => {
  assert.equal(
    reviewInProgress({
      statusCheckRollup: [{ name: REVIEW_CHECK_NAME, status: "IN_PROGRESS", conclusion: null }],
    }),
    true,
  );
  assert.equal(
    reviewInProgress({
      statusCheckRollup: [
        { name: REVIEW_CHECK_NAME, status: "COMPLETED", conclusion: "SUCCESS" },
        { name: "smoke", status: "IN_PROGRESS", conclusion: null },
      ],
    }),
    false,
  );
  assert.equal(reviewInProgress({ statusCheckRollup: null }), false);
});

test("reviewInProgress covers every Claude Review job", () => {
  for (const name of ["pair", "review (opus)", "review (sonnet)"]) {
    assert.equal(
      reviewInProgress({
        statusCheckRollup: [{ name, workflowName: CLAUDE_REVIEW_WORKFLOW, status: "QUEUED", conclusion: null }],
      }),
      true,
    );
  }
});

test("rollupOk ignores reviewer checks: a failed review job is not red CI", () => {
  assert.equal(
    rollupOk({
      statusCheckRollup: [
        { name: "python", status: "COMPLETED", conclusion: "SUCCESS" },
        { name: "review (opus)", workflowName: CLAUDE_REVIEW_WORKFLOW, status: "COMPLETED", conclusion: "FAILURE" },
        { name: REVIEW_CHECK_NAME, status: "COMPLETED", conclusion: "FAILURE" },
      ],
    }),
    true,
  );
  assert.equal(
    rollupOk({
      statusCheckRollup: [{ name: "review (opus)", workflowName: CLAUDE_REVIEW_WORKFLOW, status: "COMPLETED", conclusion: "SUCCESS" }],
    }),
    null,
  );
});

test("a job named review in another workflow is still CI", () => {
  const other = { name: "review", workflowName: "test", status: "COMPLETED", conclusion: "FAILURE" };
  assert.equal(rollupOk({ statusCheckRollup: [other] }), false);
  assert.equal(reviewInProgress({ statusCheckRollup: [{ ...other, status: "IN_PROGRESS", conclusion: null }] }), false);
});

test("rollupOk is null when any CheckRun is still in progress", () => {
  assert.equal(
    rollupOk({
      statusCheckRollup: [
        { status: "COMPLETED", conclusion: "SUCCESS" },
        { status: "IN_PROGRESS", conclusion: null },
      ],
    }),
    null,
  );
});

test("rollupOk is true when every CheckRun completed successfully", () => {
  assert.equal(
    rollupOk({
      statusCheckRollup: [
        { status: "COMPLETED", conclusion: "SUCCESS" },
        { status: "COMPLETED", conclusion: "SKIPPED" },
        { status: "COMPLETED", conclusion: "NEUTRAL" },
      ],
    }),
    true,
  );
});

test("rollupOk is false when a completed CheckRun failed", () => {
  assert.equal(
    rollupOk({
      statusCheckRollup: [
        { status: "COMPLETED", conclusion: "SUCCESS" },
        { status: "COMPLETED", conclusion: "FAILURE" },
      ],
    }),
    false,
  );
});

test("rollupOk is null with an empty or missing rollup", () => {
  assert.equal(rollupOk({ statusCheckRollup: null }), null);
  assert.equal(rollupOk({ statusCheckRollup: [] }), null);
});

test("rollupOk treats legacy StatusContext PENDING as not yet decided", () => {
  assert.equal(rollupOk({ statusCheckRollup: [{ state: "PENDING" }] }), null);
  assert.equal(rollupOk({ statusCheckRollup: [{ state: "SUCCESS" }] }), true);
  assert.equal(rollupOk({ statusCheckRollup: [{ state: "FAILURE" }] }), false);
});

test("hasMergeConflict reads CONFLICTING or DIRTY", () => {
  assert.equal(hasMergeConflict({ mergeable: "CONFLICTING", mergeStateStatus: "CLEAN" }), true);
  assert.equal(hasMergeConflict({ mergeable: "MERGEABLE", mergeStateStatus: "DIRTY" }), true);
  assert.equal(hasMergeConflict({ mergeable: "MERGEABLE", mergeStateStatus: "CLEAN" }), false);
});


const HEAD = "head-sha";

const BOTS = ["opus-review-agent", "sonnet-review-agent", "cursor"];

/** Bots by login, as GraphQL types them; everyone else is a person with `association`. */
function review(login: string, state: string, oid: string, body = "", association = "OWNER"): ReviewNode {
  const isBot = BOTS.includes(login.replace(/\[bot\]$/, ""));
  return {
    state,
    body,
    author: { __typename: isBot ? "Bot" : "User", login },
    authorAssociation: isBot ? "NONE" : association,
    commit: { oid },
  };
}

const OPUS = "opus-review-agent[bot]";
const PAIR = {
  opus: OPUS,
  second: "cursor[bot]",
  bots: ["opus-review-agent[bot]", "sonnet-review-agent[bot]", "cursor[bot]"],
};

test("parseReviewers picks the second reviewer's login", () => {
  const file = "# comment\nopus: o[bot]\nsonnet: s[bot]\ncursor: cursor[bot]\nsecond: cursor\n";
  const bots = ["o[bot]", "s[bot]", "cursor[bot]"];
  assert.deepEqual(parseReviewers(file), { opus: "o[bot]", second: "cursor[bot]", bots });
  assert.deepEqual(parseReviewers(file.replace("second: cursor", "second: sonnet")), {
    opus: "o[bot]",
    second: "s[bot]",
    bots,
  });
  assert.throws(() => parseReviewers(file.replace("second: cursor", "second: both")));
  assert.throws(() => parseReviewers("opus: o[bot]\nsecond: sonnet\n"));
});

test("headVerdict: approved only when both of the pair approved the head", () => {
  assert.equal(headVerdict([review(OPUS, "APPROVED", HEAD)], HEAD, PAIR), null);
  assert.equal(headVerdict([review("cursor", "APPROVED", HEAD)], HEAD, PAIR), null);
  assert.equal(
    headVerdict([review(OPUS, "APPROVED", HEAD), review("evandelacruz", "APPROVED", HEAD)], HEAD, PAIR),
    null,
  );
  assert.equal(
    headVerdict([review(OPUS, "APPROVED", HEAD), review("cursor[bot]", "APPROVED", HEAD)], HEAD, PAIR),
    "APPROVED",
  );
  assert.equal(headVerdict([], HEAD, PAIR), null);
});

test("headVerdict compares logins with or without the [bot] suffix", () => {
  const reviews = [review("opus-review-agent", "APPROVED", HEAD), review("cursor", "APPROVED", HEAD)];
  assert.equal(headVerdict(reviews, HEAD, PAIR), "APPROVED");
});

test("headVerdict: any trusted reviewer's rejection on the head wins, a person's included", () => {
  const both = [review(OPUS, "APPROVED", HEAD), review("cursor", "APPROVED", HEAD)];
  assert.equal(headVerdict([...both, review("evandelacruz", "CHANGES_REQUESTED", HEAD)], HEAD, PAIR), "CHANGES_REQUESTED");
  assert.equal(headVerdict([...both, review("sonnet-review-agent", "CHANGES_REQUESTED", HEAD)], HEAD, PAIR), "CHANGES_REQUESTED");
});

test("headVerdict ignores reviews on an older head", () => {
  assert.equal(headVerdict([review("cursor[bot]", "CHANGES_REQUESTED", "old")], HEAD, PAIR), null);
  assert.equal(
    headVerdict([review(OPUS, "APPROVED", "old"), review("cursor", "APPROVED", HEAD)], HEAD, PAIR),
    null,
  );
});

test("headVerdict uses each reviewer's latest verdict and skips COMMENTED and PENDING", () => {
  const reviews = [
    review(OPUS, "CHANGES_REQUESTED", HEAD),
    review(OPUS, "APPROVED", HEAD),
    review(OPUS, "COMMENTED", HEAD, "inline"),
    review("cursor", "APPROVED", HEAD),
    review("evandelacruz", "COMMENTED", HEAD, "Not approving."),
    review("someone", "PENDING", HEAD),
  ];
  assert.equal(headVerdict(reviews, HEAD, PAIR), "APPROVED");
});

test("holdLock releases when body fails before the agent starts", async () => {
  let released = 0;
  await assert.rejects(
    holdLock(
      async () => {
        released++;
      },
      async () => {
        throw new Error("create failed");
      },
    ),
    /create failed/,
  );
  assert.equal(released, 1);
});

test("holdLock keeps the lock when body fails after the agent starts", async () => {
  let released = 0;
  await assert.rejects(
    holdLock(
      async () => {
        released++;
      },
      async (started) => {
        started();
        throw new Error("wait failed");
      },
    ),
    /wait failed/,
  );
  assert.equal(released, 0);
});

test("holdLock keeps the lock on success and surfaces the original error over a failed release", async () => {
  let released = 0;
  assert.equal(
    await holdLock(
      async () => {
        released++;
      },
      async () => "ok",
    ),
    "ok",
  );
  assert.equal(released, 0);
  await assert.rejects(
    holdLock(
      async () => {
        throw new Error("release failed");
      },
      async () => {
        throw new Error("send failed");
      },
    ),
    /send failed/,
  );
});

function pr(overrides: Partial<Parameters<typeof triagePrs>[0][number]> & { n: number }) {
  return {
    isDraft: false,
    verdict: "APPROVED" as const,
    hasMergeConflict: false,
    checksOk: true as boolean | null,
    reviewInProgress: false,
    ...overrides,
  };
}

function numbers(list: Array<{ n: number }>): number[] {
  return list.map((s) => s.n);
}

test("triagePrs sends an approved, green PR with a merge conflict only to the fixer list", () => {
  const t = triagePrs([pr({ n: 1, hasMergeConflict: true })]);
  assert.deepEqual(numbers(t.needsFix), [1]);
  assert.deepEqual(numbers(t.mergeReady), []);
});

test("triagePrs routes red CI and changes requested to the fixer list", () => {
  const t = triagePrs([pr({ n: 1, checksOk: false }), pr({ n: 2, verdict: "CHANGES_REQUESTED" })]);
  assert.deepEqual(numbers(t.needsFix), [1, 2]);
  assert.deepEqual(numbers(t.mergeReady), []);
});

test("triagePrs: open threads never need a fixer; approved and green is merge-ready", () => {
  // `prs` passes the open-thread count along; it never blocks a PR.
  const t = triagePrs([
    { ...pr({ n: 1 }), unresolvedReviewThreads: 3 },
    { ...pr({ n: 2, verdict: null }), unresolvedReviewThreads: 1 },
  ]);
  assert.deepEqual(numbers(t.needsFix), []);
  assert.deepEqual(numbers(t.mergeReady), [1]);
});

test("triagePrs: not merge-ready without an approval, while CI is pending, or as a draft", () => {
  const t = triagePrs([
    pr({ n: 1, verdict: null }),
    pr({ n: 2, checksOk: null }),
    pr({ n: 3, isDraft: true }),
  ]);
  assert.deepEqual(numbers(t.needsFix), []);
  assert.deepEqual(numbers(t.mergeReady), []);
});

test("triagePrs leaves out PRs whose review check is still running", () => {
  const t = triagePrs([
    pr({ n: 1, reviewInProgress: true, hasMergeConflict: true }),
    pr({ n: 2, reviewInProgress: true }),
  ]);
  assert.deepEqual(numbers(t.needsFix), []);
  assert.deepEqual(numbers(t.mergeReady), []);
});

test("trustedReview: listed bots and OWNER, MEMBER, COLLABORATOR people only", () => {
  assert.equal(trustedReview(review(OPUS, "APPROVED", HEAD), PAIR), true);
  assert.equal(trustedReview(review("sonnet-review-agent", "APPROVED", HEAD), PAIR), true);
  for (const a of ["OWNER", "MEMBER", "COLLABORATOR"]) {
    assert.equal(trustedReview(review("someone", "APPROVED", HEAD, "", a), PAIR), true);
  }
  for (const a of ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"]) {
    assert.equal(trustedReview(review("someone", "APPROVED", HEAD, "", a), PAIR), false);
  }
  const otherBot: ReviewNode = {
    state: "APPROVED",
    body: "",
    author: { __typename: "Bot", login: "some-app[bot]" },
    authorAssociation: "NONE",
    commit: { oid: HEAD },
  };
  assert.equal(trustedReview(otherBot, PAIR), false);
  // A person who registered a listed bot's login without the suffix is still a person.
  const impostor: ReviewNode = {
    state: "APPROVED",
    body: "",
    author: { __typename: "User", login: "opus-review-agent" },
    authorAssociation: "NONE",
    commit: { oid: HEAD },
  };
  assert.equal(trustedReview(impostor, PAIR), false);
  assert.equal(trustedReview({ ...impostor, author: null }, PAIR), false);
});

test("headVerdict ignores untrusted reviews, approving or rejecting", () => {
  const both = [review(OPUS, "APPROVED", HEAD), review("cursor", "APPROVED", HEAD)];
  assert.equal(
    headVerdict([...both, review("drive-by", "CHANGES_REQUESTED", HEAD, "", "NONE")], HEAD, PAIR),
    "APPROVED",
  );
  assert.equal(
    headVerdict([...both, review("drive-by", "CHANGES_REQUESTED", HEAD, "", "CONTRIBUTOR")], HEAD, PAIR),
    "APPROVED",
  );
  assert.equal(
    headVerdict([...both, review("collab", "CHANGES_REQUESTED", HEAD, "", "COLLABORATOR")], HEAD, PAIR),
    "CHANGES_REQUESTED",
  );
  const impostor: ReviewNode = {
    state: "APPROVED",
    body: "",
    author: { __typename: "User", login: "cursor" },
    authorAssociation: "NONE",
    commit: { oid: HEAD },
  };
  assert.equal(headVerdict([review(OPUS, "APPROVED", HEAD), impostor], HEAD, PAIR), null);
});

test("submittedReviewShas skips PENDING drafts and untrusted reviews", () => {
  assert.deepEqual(
    submittedReviewShas(
      [
        review("someone", "PENDING", "draft"),
        review(OPUS, "COMMENTED", "real"),
        { ...review(OPUS, "APPROVED", "x"), commit: null },
        review("drive-by", "COMMENTED", "untrusted", "", "NONE"),
        review("collab", "COMMENTED", "collab", "", "COLLABORATOR"),
      ],
      PAIR,
    ),
    ["real", "collab"],
  );
});
