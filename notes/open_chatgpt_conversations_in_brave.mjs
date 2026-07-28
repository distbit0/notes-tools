#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { copyFile, mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  activeRateLimitCooldown,
  ChatGptClient,
  UserFacingError,
} from "./chatgpt_backend_client.mjs";
import {
  listAllActiveConversations,
  timestampToMs,
} from "./chatgpt_conversation_listing.mjs";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_PATH);
const PROJECT_ROOT = path.dirname(SCRIPT_DIR);
const HOME_DIR = os.homedir();
const CONFIG_PATH = path.join(SCRIPT_DIR, "config.json");
const DEFAULT_BRAVE_ROOT = path.join(
  HOME_DIR,
  ".config/BraveSoftware/Brave-Browser",
);
const DEFAULT_BRAVE_PROFILE = "Default";
const DEFAULT_RATE_LIMIT_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/chatgpt-backend-rate-limit.json",
);
const HISTORY_GRACE_MS = 10 * 60 * 1000;
const CHROME_EPOCH_OFFSET_MS = 11_644_473_600_000;

function usage() {
  return `Usage: open_chatgpt_conversations_in_brave.mjs [options]

Open active ChatGPT conversations that have not been viewed in Brave since
their latest assistant response.

Options:
  --profile <name>           Brave profile name (default: ${DEFAULT_BRAVE_PROFILE})
  --brave-root <dir>         Brave user data root (default: ${DEFAULT_BRAVE_ROOT})
  --bearer <token>           Use this bearer token instead of Brave cookies
  --max-conversations <n>    Inspect at most n conversations
  --request-delay-ms <n>     Minimum delay between requests (default: 10000)
  --jitter-ms <n>            Additional random request delay (default: 5000)
  --help                     Show this help

Environment:
  CHATGPT_BEARER_TOKEN       Bearer token fallback for --bearer
`;
}

function loadConfiguration() {
  const configuration = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  const conversationSync = configuration.chatgptConversationSync;
  if (
    !conversationSync ||
    typeof conversationSync.braveExecutable !== "string" ||
    !conversationSync.braveExecutable.trim()
  ) {
    throw new UserFacingError(
      "notes/config.json must define chatgptConversationSync.braveExecutable.",
    );
  }
  return conversationSync;
}

function parseArgs(argv, conversationSync = loadConfiguration()) {
  const options = {
    braveRoot: DEFAULT_BRAVE_ROOT,
    braveProfile: DEFAULT_BRAVE_PROFILE,
    braveExecutable: conversationSync.braveExecutable,
    bearer: process.env.CHATGPT_BEARER_TOKEN || "",
    maxConversations: null,
    requestDelayMs: 10_000,
    jitterMs: 5000,
    projectRoot: PROJECT_ROOT,
    rateLimitStatePath: DEFAULT_RATE_LIMIT_STATE_PATH,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = () => {
      const next = argv[index + 1];
      if (!next || next.startsWith("--")) {
        throw new UserFacingError(`Missing value for ${arg}`);
      }
      index += 1;
      return next;
    };

    if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg === "--profile") {
      options.braveProfile = value();
    } else if (arg === "--brave-root") {
      options.braveRoot = path.resolve(value());
    } else if (arg === "--bearer") {
      options.bearer = value();
    } else if (arg === "--max-conversations") {
      options.maxConversations = parsePositiveInteger(arg, value());
    } else if (arg === "--request-delay-ms") {
      options.requestDelayMs = parseNonNegativeInteger(arg, value());
    } else if (arg === "--jitter-ms") {
      options.jitterMs = parseNonNegativeInteger(arg, value());
    } else {
      throw new UserFacingError(`Unknown option: ${arg}`);
    }
  }
  return options;
}

function parsePositiveInteger(flag, rawValue) {
  const parsed = Number.parseInt(rawValue, 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new UserFacingError(`${flag} must be a positive integer`);
  }
  return parsed;
}

function parseNonNegativeInteger(flag, rawValue) {
  const parsed = Number.parseInt(rawValue, 10);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new UserFacingError(`${flag} must be a non-negative integer`);
  }
  return parsed;
}

async function openChatGptConversations(options) {
  const summary = {
    discovered: 0,
    historyEntries: 0,
    conversationDetailsChecked: 0,
    tabsOpened: 0,
    skippedAlreadyViewed: 0,
    apiRequests: 0,
  };
  const cooldown = await activeRateLimitCooldown(options.rateLimitStatePath);
  if (cooldown) {
    console.warn(
      `ChatGPT backend cooldown active until ${cooldown.blockedUntil}; made no requests.`,
    );
    return {
      status: "cooldown",
      summary: { ...summary, rateLimitedUntil: cooldown.blockedUntil },
    };
  }

  const lastOpenedByConversation = await readBraveConversationHistory(options);
  summary.historyEntries = lastOpenedByConversation.size;

  const client = new ChatGptClient(options);
  await client.initialize();
  const candidates = await listAllActiveConversations(
    client,
    options.maxConversations,
  );
  summary.discovered = candidates.length;

  for (const candidate of candidates) {
    const lastOpenedAtMs = lastOpenedByConversation.get(candidate.id);
    if (lastOpenedAtMs === undefined) {
      openBraveTab(options, candidate.id);
      summary.tabsOpened += 1;
      console.log(`Opened never-visited conversation: ${candidate.title}`);
      continue;
    }

    if (candidate.updateTimeMs <= lastOpenedAtMs + HISTORY_GRACE_MS) {
      summary.skippedAlreadyViewed += 1;
      continue;
    }

    const conversation = await client.fetchBackendJson(
      `/backend-api/conversation/${encodeURIComponent(candidate.id)}`,
    );
    summary.conversationDetailsChecked += 1;
    const latestAssistantMessageAtMs =
      latestVisibleAssistantMessageTimeMs(conversation);
    if (!shouldOpenConversation(latestAssistantMessageAtMs, lastOpenedAtMs)) {
      summary.skippedAlreadyViewed += 1;
      continue;
    }

    openBraveTab(options, candidate.id);
    summary.tabsOpened += 1;
    console.log(`Opened conversation with a newer assistant reply: ${candidate.title}`);
  }

  summary.apiRequests = client.requestCount;
  return { status: "success", summary };
}

async function readBraveConversationHistory(options) {
  const profileDirectory = path.join(options.braveRoot, options.braveProfile);
  const historyPath = path.join(profileDirectory, "History");
  if (!existsSync(historyPath)) {
    throw new UserFacingError(`Brave history database not found: ${historyPath}`);
  }

  const temporaryDirectory = await mkdtemp(
    path.join(os.tmpdir(), "chatgpt-brave-history-"),
  );
  const historyCopyPath = path.join(temporaryDirectory, "History");
  try {
    await copyFile(historyPath, historyCopyPath);
    return queryConversationHistory(historyCopyPath);
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
}

function queryConversationHistory(historyPath) {
  const query = `
    SELECT url, last_visit_time / 1000 - ${CHROME_EPOCH_OFFSET_MS} AS last_opened_at_ms
    FROM urls
    WHERE (url LIKE 'https://chatgpt.com/c/%'
       OR url LIKE 'https://chat.openai.com/c/%')
      AND last_visit_time / 1000 > ${CHROME_EPOCH_OFFSET_MS}
  `;
  const result = spawnSync("sqlite3", ["-json", historyPath, query], {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  if (result.status !== 0) {
    const detail = (result.stderr || result.error?.message || "unknown error").trim();
    throw new UserFacingError(`Could not read Brave history: ${detail}`);
  }

  const lastOpenedByConversation = new Map();
  for (const row of JSON.parse(result.stdout || "[]")) {
    const conversationId = conversationIdFromUrl(row.url);
    if (!conversationId || !Number.isFinite(row.last_opened_at_ms)) continue;
    const previousLastOpenedAtMs =
      lastOpenedByConversation.get(conversationId) ?? 0;
    lastOpenedByConversation.set(
      conversationId,
      Math.max(previousLastOpenedAtMs, row.last_opened_at_ms),
    );
  }
  return lastOpenedByConversation;
}

function conversationIdFromUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (!["chatgpt.com", "chat.openai.com"].includes(url.hostname)) return null;
    const match = url.pathname.match(/^\/c\/([^/]+)\/?$/);
    return match ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}

function latestVisibleAssistantMessageTimeMs(conversation) {
  const mapping = conversation.mapping || {};
  const latestAssistantMessage = visibleMessageIds(conversation)
    .map((messageId) => mapping[messageId]?.message)
    .filter(
      (message) =>
        message?.author?.role === "assistant" && !shouldSkipMessage(message),
    )
    .at(-1);
  if (!latestAssistantMessage) return null;

  const latestAssistantMessageAtMs = timestampToMs(
    latestAssistantMessage.create_time,
  );
  if (latestAssistantMessageAtMs <= 0) {
    throw new UserFacingError(
      "ChatGPT returned a latest assistant message without a valid timestamp.",
    );
  }
  return latestAssistantMessageAtMs;
}

function visibleMessageIds(conversation) {
  const mapping = conversation.mapping || {};
  if (conversation.current_node && mapping[conversation.current_node]) {
    const messageIds = [];
    let currentId = conversation.current_node;
    const seen = new Set();
    while (currentId && mapping[currentId] && !seen.has(currentId)) {
      seen.add(currentId);
      messageIds.push(currentId);
      currentId = mapping[currentId].parent;
    }
    return messageIds.reverse();
  }

  const rootId = Object.entries(mapping).find(([, node]) => node.parent == null)?.[0];
  const messageIds = [];
  let currentId = rootId;
  const seen = new Set();
  while (currentId && mapping[currentId] && !seen.has(currentId)) {
    seen.add(currentId);
    messageIds.push(currentId);
    currentId = mapping[currentId].children?.[0];
  }
  return messageIds;
}

function shouldSkipMessage(message) {
  if (message.metadata?.is_visually_hidden_from_conversation) return true;
  if (message.recipient && message.recipient !== "all") return true;
  return ["thoughts", "reasoning_recap"].includes(
    message.content?.content_type || "",
  );
}

function shouldOpenConversation(latestAssistantMessageAtMs, lastOpenedAtMs) {
  return (
    latestAssistantMessageAtMs !== null &&
    latestAssistantMessageAtMs > lastOpenedAtMs + HISTORY_GRACE_MS
  );
}

function openBraveTab(options, conversationId) {
  const conversationUrl = `https://chatgpt.com/c/${encodeURIComponent(
    conversationId,
  )}`;
  const result = spawnSync(
    options.braveExecutable,
    [`--profile-directory=${options.braveProfile}`, "--new-tab", conversationUrl],
    { encoding: "utf8" },
  );
  if (result.status === 0) return;

  const detail = (result.stderr || result.error?.message || "unknown error").trim();
  throw new UserFacingError(
    `Could not open ChatGPT conversation in Brave: ${detail}`,
  );
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log(usage());
    return;
  }

  const result = await openChatGptConversations(options);
  console.log(
    [
      `Discovered: ${result.summary.discovered}`,
      `Brave history entries: ${result.summary.historyEntries}`,
      `Conversation details checked: ${result.summary.conversationDetailsChecked}`,
      `Tabs opened: ${result.summary.tabsOpened}`,
      `Skipped already viewed: ${result.summary.skippedAlreadyViewed}`,
      `API requests: ${result.summary.apiRequests}`,
      ...(result.summary.rateLimitedUntil
        ? [`Rate limited until: ${result.summary.rateLimitedUntil}`]
        : []),
    ].join("\n"),
  );
}

if (process.argv[1] && path.resolve(process.argv[1]) === SCRIPT_PATH) {
  main().catch((error) => {
    if (error instanceof UserFacingError) {
      console.error(`ERROR: ${error.message}`);
    } else {
      console.error(error.stack || error.message);
    }
    process.exitCode = 1;
  });
}

export {
  conversationIdFromUrl,
  latestVisibleAssistantMessageTimeMs,
  openChatGptConversations,
  parseArgs,
  queryConversationHistory,
  readBraveConversationHistory,
  shouldOpenConversation,
};
