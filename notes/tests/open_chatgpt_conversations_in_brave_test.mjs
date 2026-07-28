import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  normalizeScanWatermarks,
  parseArgs,
  readBraveConversationHistory,
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
});
