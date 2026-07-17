import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  activeRateLimitCooldown,
  ChatGptClient,
  seedRateLimitCooldown,
} from "../chatgpt_backend_client.mjs";

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const RATE_LIMIT_OBSERVATION = JSON.parse(
  await readFile(path.join(TEST_DIR, "chatgpt_rate_limit_observation.json"), "utf8"),
);

function clientOptions(rateLimitStatePath) {
  return {
    bearer: "",
    jitterMs: 0,
    rateLimitStatePath,
    requestDelayMs: 0,
  };
}

test("one observed 429 opens the shared cooldown without retrying", async (t) => {
  const temporaryDirectory = await mkdtemp(
    path.join(tmpdir(), "chatgpt-rate-limit-test-"),
  );
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const rateLimitStatePath = path.join(temporaryDirectory, "rate-limit.json");
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = async () => {
    requestCount += 1;
    return new Response("", { status: RATE_LIMIT_OBSERVATION.status });
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const client = new ChatGptClient(clientOptions(rateLimitStatePath));
  await assert.rejects(
    client.fetchBackendJson(RATE_LIMIT_OBSERVATION.requestPath),
    /paused until/,
  );
  assert.equal(requestCount, 1);
  assert.equal(client.requestCount, 1);

  const state = JSON.parse(await readFile(rateLimitStatePath, "utf8"));
  assert.equal(
    Date.parse(state.blockedUntil) - Date.parse(state.observedAt),
    24 * 60 * 60 * 1000,
  );

  const blockedClient = new ChatGptClient(clientOptions(rateLimitStatePath));
  await assert.rejects(
    blockedClient.fetchBackendJson(RATE_LIMIT_OBSERVATION.requestPath),
    /paused until/,
  );
  assert.equal(requestCount, 1);
  assert.equal(blockedClient.requestCount, 0);
});

test("seeded cooldown expires exactly 24 hours after the real observation", async (t) => {
  const temporaryDirectory = await mkdtemp(
    path.join(tmpdir(), "chatgpt-rate-limit-seed-test-"),
  );
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const rateLimitStatePath = path.join(temporaryDirectory, "rate-limit.json");
  const state = await seedRateLimitCooldown(
    rateLimitStatePath,
    RATE_LIMIT_OBSERVATION.observedAt,
    RATE_LIMIT_OBSERVATION.requestPath,
  );

  assert.notEqual(
    await activeRateLimitCooldown(
      rateLimitStatePath,
      Date.parse(state.blockedUntil) - 1,
    ),
    null,
  );
  assert.equal(
    await activeRateLimitCooldown(
      rateLimitStatePath,
      Date.parse(state.blockedUntil),
    ),
    null,
  );
});
