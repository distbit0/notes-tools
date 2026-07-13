import assert from "node:assert/strict";
import test from "node:test";

import {
  conversationToMarkdown,
  isConversationCurrent,
  messageIdsToPersist,
} from "../chatgpt_convos_to_notes.mjs";

function conversation(messages) {
  const mapping = {
    root: { id: "root", parent: null, children: [] },
  };
  let parent = "root";
  for (const message of messages) {
    mapping[parent].children = [message.id];
    mapping[message.id] = {
      id: message.id,
      parent,
      children: [],
      message: {
        id: message.id,
        author: { role: message.role },
        recipient: "all",
        create_time: message.createTime,
        content: { content_type: "text", parts: [message.text] },
        metadata: {},
      },
    };
    parent = message.id;
  }
  return {
    id: "conversation-1",
    title: "Conversation",
    create_time: 1000,
    update_time: 3000,
    current_node: parent,
    mapping,
  };
}

test("matching ledger state skips even if the local markdown was deleted", () => {
  assert.equal(
    isConversationCurrent({
      conversations: {
        "conversation-1": {
          folderName: "deleted-locally",
          updateTimeMs: 3000,
          seenMessageIds: ["root", "m1"],
        },
      },
    }, {
      id: "conversation-1",
      updateTimeMs: 3000,
    }),
    true,
  );
});

test("legacy state without a message ledger is migrated without rewriting messages", () => {
  const data = conversation([
    { id: "m1", role: "user", createTime: 1, text: "already saved" },
    { id: "m2", role: "assistant", createTime: 2, text: "also already saved" },
  ]);
  const legacyRecord = {
    id: "conversation-1",
    folderName: "deleted-locally",
    updateTimeMs: 1000,
  };

  assert.equal(
    isConversationCurrent({
      conversations: {
        "conversation-1": legacyRecord,
      },
    }, {
      id: "conversation-1",
      updateTimeMs: 3000,
    }),
    false,
  );
  assert.deepEqual(messageIdsToPersist(data, legacyRecord), []);
});

test("message ledger only persists unseen messages", () => {
  const data = conversation([
    { id: "m1", role: "user", createTime: 1, text: "old" },
    { id: "m2", role: "assistant", createTime: 2, text: "new" },
  ]);

  assert.deepEqual(
    messageIdsToPersist(data, {
      id: "conversation-1",
      seenMessageIds: ["root", "m1"],
      updateTimeMs: 1,
    }),
    ["m2"],
  );
});

test("empty message ledger persists visible messages", () => {
  const data = conversation([
    { id: "m1", role: "user", createTime: 1, text: "first visible message" },
    { id: "m2", role: "assistant", createTime: 2, text: "second visible message" },
  ]);

  assert.deepEqual(
    messageIdsToPersist(data, {
      id: "conversation-1",
      seenMessageIds: [],
      updateTimeMs: 1,
    }),
    ["m1", "m2"],
  );
});

test("markdown can render only new message ids", () => {
  const data = conversation([
    { id: "m1", role: "user", createTime: 1, text: "do not re-add" },
    { id: "m2", role: "assistant", createTime: 2, text: "fresh reply" },
  ]);

  const markdown = conversationToMarkdown(
    data,
    {
      id: "conversation-1",
      title: "Conversation",
      createTimeMs: 1000,
      updateTimeMs: 3000,
      project: null,
    },
    {},
    ["m2"],
  );

  assert.match(markdown, /fresh reply/);
  assert.doesNotMatch(markdown, /do not re-add/);
});
