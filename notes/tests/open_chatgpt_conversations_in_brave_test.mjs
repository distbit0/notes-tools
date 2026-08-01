import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  appendUniqueConversationUrls,
  deliveryModeForScanState,
  normalizeScanWatermarks,
  parseArgs,
  readBraveConversationHistory,
  readQueuedConversationIds,
} from "../open_chatgpt_conversations_in_brave.mjs";

test("reads real ChatGPT visits from the configured Brave profile", async () => {
  const options = parseArgs([]);
  const lastOpenedByConversation = await readBraveConversationHistory(options);

  assert.equal(lastOpenedByConversation.size > 0, true);
  for (const [conversationId, lastOpenedAtMs] of lastOpenedByConversation) {
    assert.equal(conversationId.length > 0, true);
    assert.equal(Number.isFinite(lastOpenedAtMs), true);
    assert.equal(lastOpenedAtMs > 0, true);
  }
});

test("accepts the real prior browser scan watermarks", async () => {
  const priorStatePath = path.join(
    os.homedir(),
    ".local/state/chatgpt-browser-actions/state.json",
  );
  const priorState = JSON.parse(await readFile(priorStatePath, "utf8"));

  assert.deepEqual(
    normalizeScanWatermarks(priorState.scanWatermarks),
    priorState.scanWatermarks,
  );
  assert.equal(deliveryModeForScanState(priorState), "browser");
});

test("queues only the initial full scan", () => {
  assert.equal(deliveryModeForScanState(null), "queue");
});

test("persists real Brave conversation URLs without duplicates", async (context) => {
  const temporaryDirectory = await mkdtemp(
    path.join(os.tmpdir(), "chatgpt-conversation-urls-test-"),
  );
  context.after(() => rm(temporaryDirectory, { recursive: true, force: true }));

  const lastOpenedByConversation = await readBraveConversationHistory(
    parseArgs([]),
  );
  const urls = [...lastOpenedByConversation.keys()]
    .slice(0, 2)
    .map((conversationId) => `https://chatgpt.com/c/${conversationId}`);
  assert.equal(urls.length, 2);

  const outputPath = path.join(temporaryDirectory, "conversations.txt");
  assert.equal(await appendUniqueConversationUrls(outputPath, urls), 2);
  assert.equal(await appendUniqueConversationUrls(outputPath, urls), 2);
  assert.deepEqual(
    (await readFile(outputPath, "utf8")).trim().split("\n"),
    urls,
  );
  assert.deepEqual(
    await readQueuedConversationIds(outputPath),
    new Set([...lastOpenedByConversation.keys()].slice(0, 2)),
  );
});
