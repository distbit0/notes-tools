#!/usr/bin/env node
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
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
import {
  conversationToMarkdown,
  exportConversation,
  extractFileReferences,
  messageIdsToPersist,
} from "./chatgpt_conversation_export.mjs";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_PATH);
const MISC_ROOT = path.dirname(SCRIPT_DIR);
const HOME_DIR = os.homedir();
const CONFIG_PATH = path.join(SCRIPT_DIR, "config.json");
const DEFAULT_OUTPUT_ROOT = path.join(HOME_DIR, "notes/chatgpt-conversations");
const DEFAULT_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/chatgpt-convos-to-notes/state.json",
);
const DEFAULT_RATE_LIMIT_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/chatgpt-backend-rate-limit.json",
);
const DEFAULT_BRAVE_ROOT = path.join(
  HOME_DIR,
  ".config/BraveSoftware/Brave-Browser",
);
const DEFAULT_BRAVE_PROFILE = "Default";
const DEFAULT_CUTOFF_ISO = "2026-05-27T00:00:00+07:00";
const TIME_ZONE = "Asia/Ho_Chi_Minh";

function usage() {
  return `Usage: chatgpt_convos_to_notes.mjs [options]

Archive active ChatGPT conversations newer than the cutoff into ~/notes.

Options:
  --output <dir>             Output root (default: ${DEFAULT_OUTPUT_ROOT})
  --state <file>             Archive ledger (default: ${DEFAULT_STATE_PATH})
  --cutoff <iso>             Include conversations updated at/after this time
                             (default: ${DEFAULT_CUTOFF_ISO})
  --profile <name>           Brave profile name (default: ${DEFAULT_BRAVE_PROFILE})
  --brave-root <dir>         Brave user data root (default: ${DEFAULT_BRAVE_ROOT})
  --bearer <token>           Use this bearer token instead of Brave cookies
  --max-conversations <n>    Process at most n conversations this run
  --request-delay-ms <n>     Minimum delay between requests (default: 10000)
  --jitter-ms <n>            Additional random request delay (default: 5000)
  --force-run                Bypass the local twice/day and 6-hour start gate
  --status                   Show local run-gate status without network access
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
    outputRoot: DEFAULT_OUTPUT_ROOT,
    statePath: DEFAULT_STATE_PATH,
    rateLimitStatePath: DEFAULT_RATE_LIMIT_STATE_PATH,
    cutoffIso: DEFAULT_CUTOFF_ISO,
    braveRoot: DEFAULT_BRAVE_ROOT,
    braveProfile: DEFAULT_BRAVE_PROFILE,
    requestDelayMs: 10_000,
    jitterMs: 5000,
    minRunSpacingHours: 6,
    maxRunsPerDay: 2,
    maxConversations: null,
    statusOnly: false,
    forceRun: false,
    bearer: process.env.CHATGPT_BEARER_TOKEN || "",
    braveExecutable: conversationSync.braveExecutable,
    projectRoot: MISC_ROOT,
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
    } else if (arg === "--output") {
      options.outputRoot = path.resolve(value());
    } else if (arg === "--state") {
      options.statePath = path.resolve(value());
    } else if (arg === "--cutoff") {
      options.cutoffIso = value();
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
    } else if (arg === "--status") {
      options.statusOnly = true;
    } else if (arg === "--force-run") {
      options.forceRun = true;
    } else {
      throw new UserFacingError(`Unknown option: ${arg}`);
    }
  }

  options.cutoffMs = Date.parse(options.cutoffIso);
  if (!Number.isFinite(options.cutoffMs)) {
    throw new UserFacingError(`Invalid --cutoff value: ${options.cutoffIso}`);
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

function defaultState() {
  return {
    version: 1,
    runHistory: [],
    conversations: {},
    projects: {},
  };
}

async function loadState(statePath) {
  if (!existsSync(statePath)) return defaultState();

  const state = JSON.parse(await readFile(statePath, "utf8"));
  return {
    ...defaultState(),
    ...state,
    runHistory: Array.isArray(state.runHistory) ? state.runHistory : [],
    conversations:
      state.conversations && typeof state.conversations === "object"
        ? state.conversations
        : {},
    projects: state.projects && typeof state.projects === "object" ? state.projects : {},
  };
}

async function saveState(statePath, state) {
  await mkdir(path.dirname(statePath), { recursive: true });
  const tmpPath = path.join(
    path.dirname(statePath),
    `.${path.basename(statePath)}.${process.pid}.${Date.now()}.tmp`,
  );
  await writeFile(tmpPath, `${JSON.stringify(state, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await rename(tmpPath, statePath);
}

function dateKeyInTimeZone(date, timeZone = TIME_ZONE) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function checkRunGate(state, now, options) {
  const startedRuns = state.runHistory.filter((run) => run.startedAt);
  const todayKey = dateKeyInTimeZone(now);
  const todaysRuns = startedRuns.filter(
    (run) => dateKeyInTimeZone(new Date(run.startedAt)) === todayKey,
  );
  if (todaysRuns.length >= options.maxRunsPerDay) {
    throw new UserFacingError(
      `Run blocked: ${todaysRuns.length} sync runs already started on ${todayKey}.`,
    );
  }

  const latestStartedAt = startedRuns
    .map((run) => Date.parse(run.startedAt))
    .filter(Number.isFinite)
    .sort((left, right) => right - left)[0];
  if (latestStartedAt) {
    const elapsedMs = now.getTime() - latestStartedAt;
    const minSpacingMs = options.minRunSpacingHours * 60 * 60 * 1000;
    if (elapsedMs < minSpacingMs) {
      const nextRun = new Date(latestStartedAt + minSpacingMs).toISOString();
      throw new UserFacingError(
        `Run blocked: last sync started ${formatDuration(elapsedMs)} ago. ` +
          `Next allowed start: ${nextRun}.`,
      );
    }
  }
}

function formatRunGateStatus(state, now, options) {
  try {
    checkRunGate(state, now, options);
    return "allowed";
  } catch (error) {
    if (error instanceof UserFacingError) return error.message;
    throw error;
  }
}

function formatDuration(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function recordRunStart(state, options, now) {
  const run = {
    id: randomUUID(),
    startedAt: now.toISOString(),
    cutoffIso: options.cutoffIso,
    forced: options.forceRun,
    status: "running",
    summary: {},
  };
  state.runHistory.push(run);
  state.runHistory = state.runHistory.slice(-30);
  return run;
}

async function finalizeRun(statePath, state, run, status, summary) {
  run.finishedAt = new Date().toISOString();
  run.status = status;
  run.summary = summary;
  await saveState(statePath, state);
}

async function syncChatGptConversations(options) {
  await mkdir(options.outputRoot, { recursive: true });
  const state = await loadState(options.statePath);
  const now = new Date();
  const summary = {
    discovered: 0,
    skippedUnchanged: 0,
    exported: 0,
    attachmentsDownloaded: 0,
    attachmentWarnings: 0,
    apiRequests: 0,
  };

  if (options.statusOnly) {
    const gateStatus = options.forceRun
      ? `forced; normal gate would be: ${formatRunGateStatus(state, now, options)}`
      : formatRunGateStatus(state, now, options);
    console.log(`Run gate: ${gateStatus}`);
    console.log(`Output: ${options.outputRoot}`);
    console.log(`State: ${options.statePath}`);
    console.log(`Cutoff: ${new Date(options.cutoffMs).toISOString()}`);
    return { status: "status" };
  }

  const cooldown = await activeRateLimitCooldown(options.rateLimitStatePath);
  if (cooldown) return cooldownResult(cooldown, summary);

  if (!options.forceRun) checkRunGate(state, now, options);
  const run = recordRunStart(state, options, now);
  await saveState(options.statePath, state);
  const client = new ChatGptClient(options);

  try {
    await client.initialize();

    const projects = await fetchProjects(client);
    const { candidates } = await collectConversationCandidates(
      client,
      options,
      projects,
    );
    summary.discovered = candidates.length;

    for (const candidate of candidates) {
      const conversationIsCurrent = isConversationCurrent(state, candidate);
      if (conversationIsCurrent) {
        summary.skippedUnchanged += 1;
        continue;
      }

      console.log(`Exporting ${candidate.title || candidate.id}`);
      const conversation = await client.fetchBackendJson(
        `/backend-api/conversation/${encodeURIComponent(candidate.id)}`,
      );
      const result = await exportConversation(
        client,
        options.outputRoot,
        state,
        candidate,
        conversation,
      );
      if (result.messagesWritten > 0) {
        summary.exported += 1;
      } else {
        summary.skippedUnchanged += 1;
      }
      summary.attachmentsDownloaded += result.attachmentsDownloaded;
      summary.attachmentWarnings += result.attachmentWarnings;
      await saveState(options.statePath, state);
    }

    summary.apiRequests = client.requestCount;
    await finalizeRun(options.statePath, state, run, "success", summary);
    return { status: "success", summary };
  } catch (error) {
    summary.apiRequests = client.requestCount;
    await finalizeRun(options.statePath, state, run, "failed", {
      ...summary,
      error: error.message,
    });
    throw error;
  }
}

function cooldownResult(cooldown, summary) {
  console.warn(
    `ChatGPT backend cooldown active until ${cooldown.blockedUntil}; made no requests.`,
  );
  return {
    status: "cooldown",
    summary: {
      ...summary,
      apiRequests: 0,
      rateLimitedUntil: cooldown.blockedUntil,
    },
  };
}

function isConversationCurrent(state, candidate) {
  const record = state.conversations[candidate.id];
  return Boolean(
    record?.folderName &&
      Array.isArray(record.seenMessageIds) &&
      record.updateTimeMs === candidate.updateTimeMs,
  );
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log(usage());
    return;
  }

  const result = await syncChatGptConversations(options);
  if (result.summary) {
    console.log(
      [
        `Discovered: ${result.summary.discovered}`,
        `Exported: ${result.summary.exported}`,
        `Skipped unchanged: ${result.summary.skippedUnchanged}`,
        `Attachments downloaded: ${result.summary.attachmentsDownloaded}`,
        `Attachment warnings: ${result.summary.attachmentWarnings}`,
        `API requests: ${result.summary.apiRequests}`,
        ...(result.summary.rateLimitedUntil
          ? [`Rate limited until: ${result.summary.rateLimitedUntil}`]
          : []),
      ].join("\n"),
    );
  }
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
  ChatGptClient,
  conversationToMarkdown,
  dateKeyInTimeZone,
  defaultState,
  extractFileReferences,
  formatRunGateStatus,
  isConversationCurrent,
  messageIdsToPersist,
  parseArgs,
  syncChatGptConversations,
  timestampToMs,
};
