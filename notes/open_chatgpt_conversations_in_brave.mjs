#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  activeRateLimitCooldown,
  ChatGptClient,
  UserFacingError,
} from "./chatgpt_backend_client.mjs";
import {
  collectConversationCandidates,
  fetchProjects,
  timestampToMs,
} from "./chatgpt_conversation_listing.mjs";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_PATH);
const PROJECT_ROOT = path.dirname(SCRIPT_DIR);
const HOME_DIR = os.homedir();
const DEFAULT_BRAVE_ROOT = path.join(
  HOME_DIR,
  ".config/BraveSoftware/Brave-Browser",
);
const DEFAULT_BRAVE_PROFILE = "Default";
const DEFAULT_RATE_LIMIT_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/chatgpt-backend-rate-limit.json",
);
const DEFAULT_SCAN_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/open-chatgpt-conversations-in-brave/state.json",
);
const DEFAULT_OUTPUT_PATH = path.join(
  HOME_DIR,
  ".local/state/open-chatgpt-conversations-in-brave/conversations-to-open.txt",
);
const DEFAULT_RECOVERY_TITLES_PATH = path.join(
  HOME_DIR,
  ".local/state/open-chatgpt-conversations-in-brave/recovered-opened-titles.txt",
);
const HISTORY_GRACE_MS = 10 * 60 * 1000;
const CHROME_EPOCH_OFFSET_MS = 11_644_473_600_000;

function usage() {
  return `Usage: open_chatgpt_conversations_in_brave.mjs [options]

List active ChatGPT conversations that have not been viewed in Brave since
their latest assistant response. URLs are written to a persistent, deduplicated
text file; this script never opens browser tabs.

Options:
  --state <file>             Successful-scan ledger
                             (default: ${DEFAULT_SCAN_STATE_PATH})
  --output <file>            Text file receiving conversation URLs
                             (default: ${DEFAULT_OUTPUT_PATH})
  --include-titles <file>    Also include conversations identified by exact
                             titles or ChatGPT URLs in this file.
                             An existing ${DEFAULT_RECOVERY_TITLES_PATH}
                             is used automatically for an unfinished full scan.
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

function parseArgs(argv) {
  const options = {
    braveRoot: DEFAULT_BRAVE_ROOT,
    braveProfile: DEFAULT_BRAVE_PROFILE,
    bearer: process.env.CHATGPT_BEARER_TOKEN || "",
    includeTitlesPath: null,
    maxConversations: null,
    outputPath: DEFAULT_OUTPUT_PATH,
    requestDelayMs: 10_000,
    jitterMs: 5000,
    projectRoot: PROJECT_ROOT,
    rateLimitStatePath: DEFAULT_RATE_LIMIT_STATE_PATH,
    scanStatePath: DEFAULT_SCAN_STATE_PATH,
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
    } else if (arg === "--state") {
      options.scanStatePath = path.resolve(value());
    } else if (arg === "--output") {
      options.outputPath = path.resolve(value());
    } else if (arg === "--include-titles") {
      options.includeTitlesPath = path.resolve(value());
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
  const scanState = await loadScanState(options.scanStatePath);
  const includeTitlesPath =
    options.includeTitlesPath ||
    (!scanState && existsSync(DEFAULT_RECOVERY_TITLES_PATH)
      ? DEFAULT_RECOVERY_TITLES_PATH
      : null);
  if (options.includeTitlesPath && scanState) {
    throw new UserFacingError(
      "--include-titles requires a full scan with no existing scan ledger.",
    );
  }
  if (options.includeTitlesPath && options.maxConversations !== null) {
    throw new UserFacingError(
      "--include-titles cannot be combined with --max-conversations.",
    );
  }
  const summary = {
    scanMode: scanState ? "incremental" : "full",
    discovered: 0,
    historyEntries: 0,
    conversationDetailsChecked: 0,
    recoveredUrls: 0,
    urlsQueued: 0,
    outputUrls: 0,
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
  const projects = await fetchProjects(client);
  const { candidates, scanWatermarks } = await collectConversationCandidates(
    client,
    { cutoffMs: 0, maxConversations: options.maxConversations },
    projects,
    scanState?.scanWatermarks,
  );
  summary.discovered = candidates.length;
  const recoveredConversationIds = includeTitlesPath
    ? resolveConversationReferences(
        candidates,
        await readRecoveryList(includeTitlesPath),
      )
    : new Set();
  summary.recoveredUrls = recoveredConversationIds.size;
  const urlsToQueue = [];

  for (const candidate of candidates) {
    if (recoveredConversationIds.has(candidate.id)) {
      urlsToQueue.push(conversationUrl(candidate.id));
      console.log(`Queued recovered conversation: ${candidate.title}`);
      continue;
    }

    const lastOpenedAtMs = lastOpenedByConversation.get(candidate.id);
    if (lastOpenedAtMs === undefined) {
      urlsToQueue.push(conversationUrl(candidate.id));
      console.log(`Queued never-visited conversation: ${candidate.title}`);
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

    urlsToQueue.push(conversationUrl(candidate.id));
    console.log(
      `Queued conversation with a newer assistant reply: ${candidate.title}`,
    );
  }

  summary.apiRequests = client.requestCount;
  summary.urlsQueued = urlsToQueue.length;
  summary.outputUrls = await appendUniqueConversationUrls(
    options.outputPath,
    urlsToQueue,
  );
  if (options.maxConversations === null) {
    await saveScanState(options.scanStatePath, {
      version: 1,
      lastSuccessfulRunAt: new Date().toISOString(),
      scanWatermarks,
    });
  }
  return { status: "success", summary };
}

async function readRecoveryList(recoveryListPath) {
  if (!existsSync(recoveryListPath)) {
    throw new UserFacingError(
      `Conversation recovery list not found: ${recoveryListPath}`,
    );
  }
  const references = (await readFile(recoveryListPath, "utf8"))
    .split(/\r?\n/)
    .filter(Boolean);
  if (references.length === 0) {
    throw new UserFacingError(
      `Conversation recovery list is empty: ${recoveryListPath}`,
    );
  }
  if (new Set(references).size !== references.length) {
    throw new UserFacingError(
      `Conversation recovery list contains duplicate entries: ${recoveryListPath}`,
    );
  }
  return references;
}

function resolveConversationReferences(candidates, references) {
  const candidatesById = new Map(
    candidates.map((candidate) => [candidate.id, candidate]),
  );
  const candidatesByTitle = new Map();
  for (const candidate of candidates) {
    const matches = candidatesByTitle.get(candidate.title) || [];
    matches.push(candidate);
    candidatesByTitle.set(candidate.title, matches);
  }

  const resolvedIds = new Set();
  for (const reference of references) {
    const conversationId = conversationIdFromUrl(reference);
    if (conversationId) {
      if (!candidatesById.has(conversationId)) {
        throw new UserFacingError(
          `Recovered URL does not identify an active conversation: ${reference}`,
        );
      }
      resolvedIds.add(conversationId);
      continue;
    }

    const matches = candidatesByTitle.get(reference) || [];
    if (matches.length !== 1) {
      throw new UserFacingError(
        `Recovered title must match exactly one active conversation; ` +
          `${JSON.stringify(reference)} matched ${matches.length}.`,
      );
    }
    resolvedIds.add(matches[0].id);
  }
  return resolvedIds;
}

async function appendUniqueConversationUrls(outputPath, urls) {
  const existingUrls = existsSync(outputPath)
    ? (await readFile(outputPath, "utf8")).split(/\r?\n/).filter(Boolean)
    : [];
  for (const url of existingUrls) {
    if (!conversationIdFromUrl(url)) {
      throw new UserFacingError(
        `Conversation output contains an invalid URL: ${outputPath}`,
      );
    }
  }

  const combinedUrls = [...new Set([...existingUrls, ...urls])];
  await mkdir(path.dirname(outputPath), { recursive: true });
  const temporaryPath = path.join(
    path.dirname(outputPath),
    `.${path.basename(outputPath)}.${process.pid}.${Date.now()}.tmp`,
  );
  await writeFile(temporaryPath, `${combinedUrls.join("\n")}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await rename(temporaryPath, outputPath);
  return combinedUrls.length;
}

async function loadScanState(scanStatePath) {
  if (!existsSync(scanStatePath)) return null;
  const state = JSON.parse(await readFile(scanStatePath, "utf8"));
  if (state.version !== 1) {
    throw new UserFacingError(
      `Unsupported ChatGPT opener state version: ${state.version}`,
    );
  }
  return {
    ...state,
    scanWatermarks: normalizeScanWatermarks(state.scanWatermarks),
  };
}

function normalizeScanWatermarks(scanWatermarks) {
  if (!scanWatermarks || typeof scanWatermarks !== "object") {
    throw new UserFacingError("ChatGPT opener scan watermarks must be an object.");
  }
  const normal = scanWatermarks.normal ?? null;
  if (normal !== null && !Number.isFinite(normal)) {
    throw new UserFacingError(
      "ChatGPT opener normal scan watermark must be a timestamp.",
    );
  }
  const projects = scanWatermarks.projects ?? {};
  if (!projects || typeof projects !== "object" || Array.isArray(projects)) {
    throw new UserFacingError(
      "ChatGPT opener project scan watermarks must be an object.",
    );
  }
  for (const [projectId, updateTimeMs] of Object.entries(projects)) {
    if (!Number.isFinite(updateTimeMs)) {
      throw new UserFacingError(
        `ChatGPT opener project watermark ${projectId} must be a timestamp.`,
      );
    }
  }
  return { normal, projects };
}

async function saveScanState(scanStatePath, state) {
  await mkdir(path.dirname(scanStatePath), { recursive: true });
  const temporaryPath = path.join(
    path.dirname(scanStatePath),
    `.${path.basename(scanStatePath)}.${process.pid}.${Date.now()}.tmp`,
  );
  await writeFile(temporaryPath, `${JSON.stringify(state, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await rename(temporaryPath, scanStatePath);
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

function conversationUrl(conversationId) {
  return `https://chatgpt.com/c/${encodeURIComponent(conversationId)}`;
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
      `Scan mode: ${result.summary.scanMode}`,
      `Discovered: ${result.summary.discovered}`,
      `Brave history entries: ${result.summary.historyEntries}`,
      `Conversation details checked: ${result.summary.conversationDetailsChecked}`,
      `Recovered URLs queued: ${result.summary.recoveredUrls}`,
      `URLs queued this run: ${result.summary.urlsQueued}`,
      `URLs in output file: ${result.summary.outputUrls}`,
      `Output file: ${options.outputPath}`,
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
  appendUniqueConversationUrls,
  latestVisibleAssistantMessageTimeMs,
  loadScanState,
  normalizeScanWatermarks,
  openChatGptConversations,
  parseArgs,
  queryConversationHistory,
  readBraveConversationHistory,
  resolveConversationReferences,
  shouldOpenConversation,
};
