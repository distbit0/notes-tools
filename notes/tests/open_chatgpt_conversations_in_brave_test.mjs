import assert from "node:assert/strict";
import test from "node:test";

import {
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
