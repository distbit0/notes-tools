import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { parseArgs, syncChatGptConversations } from "../chatgpt_convos_to_notes.mjs";

test("live ChatGPT sync exports one real conversation when explicitly enabled", async (t) => {
  if (process.env.CHATGPT_LIVE_TEST !== "1") {
    t.skip("set CHATGPT_LIVE_TEST=1 to run against the live ChatGPT account");
    return;
  }

  const outputRoot = await mkdtemp(path.join(tmpdir(), "chatgpt-convos-to-notes-"));
  t.after(() => rm(outputRoot, { recursive: true, force: true }));

  const options = parseArgs([
    "--output",
    outputRoot,
    "--state",
    path.join(outputRoot, "state.json"),
    "--max-conversations",
    "1",
    "--request-delay-ms",
    "4000",
    "--jitter-ms",
    "1000",
  ]);
  const result = await syncChatGptConversations(options);
  assert.equal(result.status, "success");
  assert.equal(result.summary.exported, 1);

  const entries = await readdir(outputRoot, { withFileTypes: true });
  const conversationDirs = entries.filter((entry) => entry.isDirectory());
  assert.equal(conversationDirs.length, 1);

  const markdownPath = path.join(
    outputRoot,
    conversationDirs[0].name,
    "conversation.md",
  );
  const markdown = await readFile(markdownPath, "utf8");
  assert.match(markdown, /^---\n/);
  assert.match(markdown, /chatgpt_url: "https:\/\/chatgpt\.com\/c\//);
});
