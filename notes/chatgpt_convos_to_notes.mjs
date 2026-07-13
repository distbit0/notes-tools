#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import {
  appendFile,
  mkdir,
  readFile,
  readdir,
  rename,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_PATH);
const MISC_ROOT = path.dirname(SCRIPT_DIR);
const HOME_DIR = os.homedir();
const DEFAULT_OUTPUT_ROOT = path.join(HOME_DIR, "notes/chatgpt-conversations");
const DEFAULT_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/chatgpt-convos-to-notes/state.json",
);
const DEFAULT_BRAVE_ROOT = path.join(
  HOME_DIR,
  ".config/BraveSoftware/Brave-Browser",
);
const DEFAULT_BRAVE_PROFILE = "Default";
const DEFAULT_CUTOFF_ISO = "2026-05-27T00:00:00+07:00";
const TIME_ZONE = "Asia/Ho_Chi_Minh";
const MARKDOWN_FORMAT_VERSION = 3;
const CONVERSATIONS_PAGE_SIZE = 28;
const REQUEST_RETRY_LIMIT = 3;
const REQUEST_TIMEOUT_MS = 60_000;
const PERMANENT_ATTACHMENT_STATUSES = new Set([
  "downloaded",
  "file_not_found",
  "access_denied",
]);

class UserFacingError extends Error {}
class NonRetryableRequestError extends Error {}

function usage() {
  return `Usage: chatgpt_convos_to_notes.mjs [options]

Sync active ChatGPT conversations newer than the cutoff into ~/notes.

Options:
  --output <dir>             Output root (default: ${DEFAULT_OUTPUT_ROOT})
  --state <file>             Runtime ledger (default: ${DEFAULT_STATE_PATH})
  --cutoff <iso>             Include conversations updated at/after this time
                             (default: ${DEFAULT_CUTOFF_ISO})
  --profile <name>           Brave profile name (default: ${DEFAULT_BRAVE_PROFILE})
  --brave-root <dir>         Brave user data root (default: ${DEFAULT_BRAVE_ROOT})
  --bearer <token>           Use this bearer token instead of Brave cookies
  --max-conversations <n>    Export at most n conversations this run
  --request-delay-ms <n>     Minimum delay between requests (default: 4000)
  --jitter-ms <n>            Additional random request delay (default: 1000)
  --force-run                Bypass the local twice/day and 6-hour start gate
  --status                   Show local run-gate status without network access
  --help                     Show this help

Environment:
  CHATGPT_BEARER_TOKEN       Bearer token fallback for --bearer
`;
}

function parseArgs(argv) {
  const options = {
    outputRoot: DEFAULT_OUTPUT_ROOT,
    statePath: DEFAULT_STATE_PATH,
    cutoffIso: DEFAULT_CUTOFF_ISO,
    braveRoot: DEFAULT_BRAVE_ROOT,
    braveProfile: DEFAULT_BRAVE_PROFILE,
    requestDelayMs: 4000,
    jitterMs: 1000,
    minRunSpacingHours: 6,
    maxRunsPerDay: 2,
    maxConversations: null,
    statusOnly: false,
    forceRun: false,
    bearer: process.env.CHATGPT_BEARER_TOKEN || "",
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

function buildPythonCookieReader(options) {
  return `
from __future__ import annotations

import pathlib
import sys

import browser_cookie3

brave_root = pathlib.Path(${JSON.stringify(options.braveRoot)})
profile = ${JSON.stringify(options.braveProfile)}
cookie_file = brave_root / profile / "Cookies"
if not cookie_file.exists():
    raise SystemExit(f"Brave cookie DB not found: {cookie_file}")

jar = browser_cookie3.brave(cookie_file=str(cookie_file), domain_name="chatgpt.com")
cookies = [
    cookie
    for cookie in jar
    if cookie.domain.endswith("chatgpt.com") and cookie.value
]
if not cookies:
    raise SystemExit("No chatgpt.com cookies found in Brave profile")

cookies.sort(key=lambda cookie: (cookie.domain, cookie.path, cookie.name))
sys.stdout.write("; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies))
`;
}

function readBraveCookieHeader(options) {
  const result = spawnSync(
    "uv",
    ["run", "--project", MISC_ROOT, "python", "-c", buildPythonCookieReader(options)],
    {
      encoding: "utf8",
      maxBuffer: 4 * 1024 * 1024,
    },
  );
  if (result.status !== 0) {
    const error = (result.stderr || result.stdout || "").trim();
    throw new UserFacingError(
      `Could not read Brave ChatGPT cookies: ${error || "unknown error"}`,
    );
  }
  const cookieHeader = result.stdout.trim();
  if (!cookieHeader.includes("__Secure-next-auth.session-token")) {
    throw new UserFacingError(
      "Brave profile does not contain a ChatGPT session-token cookie.",
    );
  }
  return cookieHeader;
}

class RequestThrottler {
  constructor({ requestDelayMs, jitterMs }) {
    this.requestDelayMs = requestDelayMs;
    this.jitterMs = jitterMs;
    this.nextAllowedAt = 0;
  }

  async wait() {
    const now = Date.now();
    if (this.nextAllowedAt > now) {
      await sleep(this.nextAllowedAt - now);
    }
    const jitter = this.jitterMs > 0 ? Math.floor(Math.random() * this.jitterMs) : 0;
    this.nextAllowedAt = Date.now() + this.requestDelayMs + jitter;
  }
}

class ChatGptClient {
  constructor(options) {
    this.options = options;
    this.throttler = new RequestThrottler(options);
    this.deviceId = randomUUID();
    this.accessToken = "";
    this.accountId = "";
  }

  async initialize() {
    if (this.options.bearer) {
      this.accessToken = this.options.bearer;
      this.accountId = accountIdFromBearer(this.accessToken);
      return;
    }

    const cookieHeader = readBraveCookieHeader(this.options);
    const session = await this.fetchSession(cookieHeader);
    if (!session.accessToken) {
      throw new UserFacingError(
        "ChatGPT session endpoint did not return an access token.",
      );
    }
    this.accessToken = session.accessToken;
    this.accountId = session.account?.id || accountIdFromBearer(this.accessToken);
  }

  async fetchSession(cookieHeader) {
    const response = await this.fetchWithRetry(
      "https://chatgpt.com/api/auth/session",
      {
        headers: {
          Accept: "application/json",
          Cookie: cookieHeader,
          Referer: "https://chatgpt.com/",
          "User-Agent": browserUserAgent(),
        },
      },
    );
    return response.json();
  }

  async fetchBackendJson(pathname) {
    const response = await this.fetchWithRetry(
      `https://chatgpt.com${pathname.startsWith("/") ? "" : "/"}${pathname}`,
      {
        headers: this.backendHeaders("application/json"),
      },
    );
    return response.json();
  }

  async fetchFileDownloadInfo(fileId, conversationId) {
    const response = await this.fetchWithRetry(
      `https://chatgpt.com/backend-api/files/download/${encodeURIComponent(
        fileId,
      )}?conversation_id=${encodeURIComponent(conversationId)}&inline=false`,
      {
        headers: this.backendHeaders("application/json"),
      },
      { authErrorIsFatal: false },
    );
    return response.json();
  }

  async fetchDownload(downloadUrl) {
    const response = await this.fetchWithRetry(downloadUrl, {
      headers: this.backendHeaders("*/*"),
    }, { authErrorIsFatal: false });
    return {
      buffer: Buffer.from(await response.arrayBuffer()),
      contentType: response.headers.get("content-type") || "",
    };
  }

  backendHeaders(accept) {
    const headers = {
      Accept: accept,
      Authorization: `Bearer ${this.accessToken}`,
      Origin: "https://chatgpt.com",
      Referer: "https://chatgpt.com/",
      "Content-Type": "application/json",
      "Oai-Device-Id": this.deviceId,
      "Oai-Language": "en-US",
      "User-Agent": browserUserAgent(),
    };
    if (this.accountId) headers["chatgpt-account-id"] = this.accountId;
    return headers;
  }

  async fetchWithRetry(url, init, { authErrorIsFatal = true } = {}) {
    let lastError = null;
    for (let attempt = 1; attempt <= REQUEST_RETRY_LIMIT; attempt += 1) {
      await this.throttler.wait();
      try {
        const response = await fetch(url, {
          ...init,
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        if (response.ok) return response;

        if (authErrorIsFatal && (response.status === 401 || response.status === 403)) {
          throw new UserFacingError(
            `ChatGPT request failed with HTTP ${response.status}. ` +
              "Authentication or Cloudflare access is no longer valid.",
          );
        }

        if (
          (response.status === 429 || response.status >= 500) &&
          attempt < REQUEST_RETRY_LIMIT
        ) {
          const retryAfterMs = retryAfterToMs(response.headers.get("retry-after"));
          const waitMs = retryAfterMs ?? attempt * 30_000 + randomJitter(5000);
          console.warn(
            `Warning: ChatGPT returned HTTP ${response.status}; retrying in ${Math.round(
              waitMs / 1000,
            )}s.`,
          );
          await sleep(waitMs);
          continue;
        }

        const body = await response.text().catch(() => "");
        throw new NonRetryableRequestError(
          `ChatGPT request failed with HTTP ${response.status}: ${body.slice(0, 200)}`,
        );
      } catch (error) {
        if (
          error instanceof UserFacingError ||
          error instanceof NonRetryableRequestError
        ) {
          throw error;
        }
        lastError = error;
        if (attempt < REQUEST_RETRY_LIMIT) {
          const waitMs = attempt * 30_000 + randomJitter(5000);
          console.warn(
            `Warning: request failed (${error.message}); retrying in ${Math.round(
              waitMs / 1000,
            )}s.`,
          );
          await sleep(waitMs);
          continue;
        }
      }
    }
    throw lastError;
  }
}

function browserUserAgent() {
  return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36";
}

function retryAfterToMs(rawValue) {
  if (!rawValue) return null;
  const seconds = Number.parseFloat(rawValue);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const dateMs = Date.parse(rawValue);
  if (Number.isFinite(dateMs)) return Math.max(0, dateMs - Date.now());
  return null;
}

function randomJitter(maxMs) {
  return Math.floor(Math.random() * maxMs);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function accountIdFromBearer(accessToken) {
  try {
    const payload = JSON.parse(
      Buffer.from(accessToken.split(".")[1], "base64url").toString("utf8"),
    );
    return payload["https://api.openai.com/auth"]?.chatgpt_account_id || "";
  } catch {
    return "";
  }
}

async function syncChatGptConversations(options) {
  await mkdir(options.outputRoot, { recursive: true });
  const state = await loadState(options.statePath);
  const now = new Date();

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

  if (!options.forceRun) checkRunGate(state, now, options);
  const run = recordRunStart(state, options, now);
  await saveState(options.statePath, state);

  const summary = {
    discovered: 0,
    skippedUnchanged: 0,
    exported: 0,
    attachmentsDownloaded: 0,
    attachmentWarnings: 0,
  };

  try {
    const client = new ChatGptClient(options);
    await client.initialize();

    const candidates = await collectCandidates(client, options);
    summary.discovered = candidates.length;

    for (const candidate of candidates) {
      if (isConversationCurrent(state, candidate)) {
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

    await finalizeRun(options.statePath, state, run, "success", summary);
    return { status: "success", summary };
  } catch (error) {
    await finalizeRun(options.statePath, state, run, "failed", {
      ...summary,
      error: error.message,
    });
    throw error;
  }
}

async function collectCandidates(client, options) {
  const candidatesById = new Map();
  await collectNormalCandidates(client, options, candidatesById);

  if (!hasReachedCandidateLimit(options, candidatesById)) {
    await collectProjectCandidates(client, options, candidatesById);
  }

  return [...candidatesById.values()]
    .sort((left, right) => right.updateTimeMs - left.updateTimeMs)
    .slice(0, options.maxConversations ?? undefined);
}

async function collectNormalCandidates(client, options, candidatesById) {
  let offset = 0;
  while (!hasReachedCandidateLimit(options, candidatesById)) {
    const query = new URLSearchParams({
      offset: String(offset),
      limit: String(CONVERSATIONS_PAGE_SIZE),
      order: "updated",
      is_archived: "false",
    });
    const data = await client.fetchBackendJson(
      `/backend-api/conversations?${query.toString()}`,
    );
    const items = Array.isArray(data.items) ? data.items : [];
    if (items.length === 0) break;

    for (const item of items) {
      const candidate = normalizeConversationListItem(item, null);
      if (candidate.updateTimeMs >= options.cutoffMs) {
        candidatesById.set(candidate.id, candidate);
      }
    }

    const reachedOlderPage = items.every((item) => {
      const candidate = normalizeConversationListItem(item, null);
      return candidate.updateTimeMs < options.cutoffMs;
    });
    if (reachedOlderPage || items.length < CONVERSATIONS_PAGE_SIZE) break;
    offset += items.length;
  }
}

async function collectProjectCandidates(client, options, candidatesById) {
  const projects = await fetchProjects(client);
  for (const project of projects) {
    if (hasReachedCandidateLimit(options, candidatesById)) break;
    await collectSingleProjectCandidates(client, options, candidatesById, project);
  }
}

async function fetchProjects(client) {
  const projects = [];
  let cursor = null;
  do {
    const query = new URLSearchParams({
      owned_only: "true",
      conversations_per_gizmo: "0",
    });
    if (cursor) query.set("cursor", cursor);
    const data = await client.fetchBackendJson(
      `/backend-api/gizmos/snorlax/sidebar?${query.toString()}`,
    );
    const items = Array.isArray(data.items) ? data.items : [];
    for (const item of items) {
      const gizmo = item.gizmo?.gizmo || item.gizmo;
      if (!gizmo?.id) continue;
      projects.push({
        id: gizmo.id,
        name: gizmo.display?.name || "Untitled Project",
      });
    }
    cursor = data.cursor || null;
  } while (cursor);
  return projects;
}

async function collectSingleProjectCandidates(client, options, candidatesById, project) {
  let cursor = "0";
  while (cursor && !hasReachedCandidateLimit(options, candidatesById)) {
    const data = await client.fetchBackendJson(
      `/backend-api/gizmos/${encodeURIComponent(
        project.id,
      )}/conversations?cursor=${encodeURIComponent(cursor)}`,
    );
    const items = Array.isArray(data.items) ? data.items : [];
    if (items.length === 0) break;

    for (const item of items) {
      const candidate = normalizeConversationListItem(item, project);
      if (candidate.updateTimeMs >= options.cutoffMs) {
        candidatesById.set(candidate.id, candidate);
      }
    }

    const reachedOlderPage = items.every((item) => {
      const candidate = normalizeConversationListItem(item, project);
      return candidate.updateTimeMs < options.cutoffMs;
    });
    if (reachedOlderPage) break;
    cursor = data.cursor || null;
  }
}

function hasReachedCandidateLimit(options, candidatesById) {
  return (
    options.maxConversations !== null && candidatesById.size >= options.maxConversations
  );
}

function normalizeConversationListItem(item, project) {
  const id = item.id || item.conversation_id;
  if (!id) throw new Error("Conversation list item missing id");
  const updateTimeMs = timestampToMs(item.update_time || item.updated_at);
  const createTimeMs = timestampToMs(item.create_time || item.created_at);
  return {
    id,
    title: item.title || "Untitled",
    createTimeMs,
    updateTimeMs,
    project,
  };
}

function timestampToMs(value) {
  if (typeof value === "number") return value < 10_000_000_000 ? value * 1000 : value;
  const parsed = Date.parse(value);
  if (Number.isFinite(parsed)) return parsed;
  return 0;
}

function isConversationCurrent(state, candidate) {
  const record = state.conversations[candidate.id];
  return Boolean(
    record?.folderName &&
      Array.isArray(record.seenMessageIds) &&
      record.updateTimeMs === candidate.updateTimeMs,
  );
}

async function exportConversation(
  client,
  outputRoot,
  state,
  candidate,
  conversation,
) {
  const folderName = await conversationFolderName(outputRoot, state, candidate);
  const previousRecord = state.conversations[candidate.id] || {};
  const conversationDir = path.join(outputRoot, folderName);
  const markdownPath = path.join(conversationDir, "conversation.md");
  const newMessageIds = messageIdsToPersist(conversation, previousRecord);
  const messageIdsWithRenderedContent = newMessageIds.filter((messageId) => {
    const message = conversation.mapping?.[messageId]?.message;
    return message && !shouldSkipMessage(message);
  });
  const seenMessageIds = new Set(previousRecord.seenMessageIds || []);

  const record = {
    ...previousRecord,
    id: candidate.id,
    title: candidate.title,
    updateTimeMs: candidate.updateTimeMs,
    createTimeMs: candidate.createTimeMs,
    folderName,
    project: candidate.project,
    attachments: { ...(previousRecord.attachments || {}) },
    markdownFormatVersion: MARKDOWN_FORMAT_VERSION,
    exportedAt: new Date().toISOString(),
  };

  for (const messageId of visibleMessageIds(conversation)) {
    seenMessageIds.add(messageId);
  }

  if (newMessageIds.length === 0 || messageIdsWithRenderedContent.length === 0) {
    record.seenMessageIds = [...seenMessageIds];
    state.conversations[candidate.id] = record;
    return {
      attachmentsDownloaded: 0,
      attachmentWarnings: 0,
      messagesWritten: 0,
    };
  }

  const attachmentsDir = path.join(conversationDir, "attachments");
  await mkdir(attachmentsDir, { recursive: true });

  const fileRefs = extractFileReferences(
    conversation,
    new Set(messageIdsWithRenderedContent),
  );
  const attachmentResult = await downloadAttachments(
    client,
    attachmentsDir,
    record,
    fileRefs,
    conversation.id || candidate.id,
  );

  const markdown = conversationToMarkdown(
    conversation,
    candidate,
    attachmentResult.fileMap,
    messageIdsWithRenderedContent,
  );
  const messageSections = messageSectionsToMarkdown(
    conversation,
    attachmentResult.fileMap,
    messageIdsWithRenderedContent,
  );
  if (!messageSections) {
    record.seenMessageIds = [...seenMessageIds];
    state.conversations[candidate.id] = record;
    return { ...attachmentResult, messagesWritten: 0 };
  }

  if (existsSync(markdownPath)) {
    await appendFile(markdownPath, `\n\n${messageSections}\n`, "utf8");
  } else {
    await writeFile(markdownPath, markdown, "utf8");
  }

  record.seenMessageIds = [...seenMessageIds];
  state.conversations[candidate.id] = record;
  return {
    ...attachmentResult,
    messagesWritten: messageIdsWithRenderedContent.length,
  };
}

async function conversationFolderName(outputRoot, state, candidate) {
  const existingFolder = state.conversations[candidate.id]?.folderName;
  if (existingFolder) return existingFolder;

  const shortId = shortConversationId(candidate.id);
  if (existsSync(outputRoot)) {
    for (const entry of await readdir(outputRoot, { withFileTypes: true })) {
      if (entry.isDirectory() && entry.name.endsWith(`--${shortId}`)) {
        return entry.name;
      }
    }
  }

  const dateMs = candidate.createTimeMs || candidate.updateTimeMs || Date.now();
  const datePrefix = new Date(dateMs).toISOString().slice(0, 10);
  return `${datePrefix}--${slugify(candidate.title || "untitled")}--${shortId}`;
}

function shortConversationId(id) {
  return id.replace(/[^a-zA-Z0-9]/g, "").slice(0, 8) || "unknown";
}

function slugify(value) {
  const slug = value
    .normalize("NFKD")
    .replace(/[^\x00-\x7F]/g, "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
  return slug || "untitled";
}

function messageIdsToPersist(conversation, previousRecord) {
  const visibleIds = visibleMessageIds(conversation).filter(
    (messageId) => conversation.mapping?.[messageId]?.message,
  );
  if (!previousRecord?.id) return visibleIds;

  if (!Array.isArray(previousRecord.seenMessageIds)) return [];

  const seenMessageIds = new Set(previousRecord.seenMessageIds);
  return visibleIds.filter((messageId) => !seenMessageIds.has(messageId));
}

function extractFileReferences(conversation, messageIds = null) {
  const refsById = new Map();
  const nodes = messageIds
    ? [...messageIds].map((messageId) => conversation.mapping?.[messageId])
    : Object.values(conversation.mapping || {});
  for (const node of nodes) {
    const message = node?.message;
    if (!message) continue;
    const content = message.content || {};

    for (const part of content.parts || []) {
      if (part && typeof part === "object" && part.asset_pointer) {
        addFileRef(refsById, {
          fileId: normalizeAssetPointer(part.asset_pointer),
          name: part.metadata?.name || part.metadata?.title || "",
          type: part.content_type || "asset",
        });
      }
    }

    if (content.asset_pointer) {
      addFileRef(refsById, {
        fileId: normalizeAssetPointer(content.asset_pointer),
        name: content.metadata?.name || content.metadata?.title || "",
        type: content.content_type || "asset",
      });
    }

    for (const attachment of message.metadata?.attachments || []) {
      addFileRef(refsById, {
        fileId: attachment.id || attachment.file_id,
        name: attachment.name || attachment.file_name || "",
        type: "attachment",
      });
    }

    for (const citation of message.metadata?.citations || []) {
      addFileRef(refsById, {
        fileId: citation.metadata?.file_id || citation.file_id,
        name: citation.metadata?.title || citation.title || "",
        type: "citation",
      });
    }
  }
  return [...refsById.values()];
}

function addFileRef(refsById, ref) {
  if (!ref.fileId) return;
  const existing = refsById.get(ref.fileId) || {};
  refsById.set(ref.fileId, {
    fileId: ref.fileId,
    name: existing.name || ref.name || "",
    type: existing.type || ref.type || "attachment",
  });
}

function normalizeAssetPointer(assetPointer) {
  return String(assetPointer).replace(/^(sediment|file-service):\/\//, "");
}

async function downloadAttachments(
  client,
  attachmentsDir,
  conversationRecord,
  fileRefs,
  conversationId,
) {
  const fileMap = {};
  let attachmentsDownloaded = 0;
  let attachmentWarnings = 0;

  for (const ref of fileRefs) {
    const existingRecord = conversationRecord.attachments[ref.fileId];
    if (existingRecord?.relativePath && existingRecord.status === "downloaded") {
      const absolutePath = path.join(path.dirname(attachmentsDir), existingRecord.relativePath);
      if (existsSync(absolutePath)) {
        fileMap[ref.fileId] = existingRecord.relativePath;
        continue;
      }
    }
    if (existingRecord && PERMANENT_ATTACHMENT_STATUSES.has(existingRecord.status)) {
      continue;
    }

    try {
      const downloadInfo = await client.fetchFileDownloadInfo(
        ref.fileId,
        conversationId,
      );
      if (downloadInfo.status !== "success" || !downloadInfo.download_url) {
        const status = downloadInfo.error_code || "download_url_unavailable";
        conversationRecord.attachments[ref.fileId] = {
          ...existingRecord,
          fileId: ref.fileId,
          name: ref.name,
          status,
          updatedAt: new Date().toISOString(),
        };
        attachmentWarnings += 1;
        console.warn(`Warning: attachment ${ref.fileId} unavailable (${status}).`);
        continue;
      }

      const downloaded = await client.fetchDownload(downloadInfo.download_url);
      const filename = chooseAttachmentFilename(
        attachmentsDir,
        ref,
        downloadInfo.file_name,
        downloaded.contentType,
        conversationRecord.attachments,
      );
      const relativePath = path.posix.join("attachments", filename);
      await writeFile(path.join(attachmentsDir, filename), downloaded.buffer);
      conversationRecord.attachments[ref.fileId] = {
        fileId: ref.fileId,
        name: ref.name || downloadInfo.file_name || "",
        relativePath,
        status: "downloaded",
        bytes: downloaded.buffer.length,
        updatedAt: new Date().toISOString(),
      };
      fileMap[ref.fileId] = relativePath;
      attachmentsDownloaded += 1;
    } catch (error) {
      const status = attachmentStatusForError(error);
      conversationRecord.attachments[ref.fileId] = {
        ...existingRecord,
        fileId: ref.fileId,
        name: ref.name,
        status,
        error: error.message,
        updatedAt: new Date().toISOString(),
      };
      attachmentWarnings += 1;
      console.warn(`Warning: failed to download attachment ${ref.fileId}: ${error.message}`);
      if (error instanceof UserFacingError) throw error;
    }
  }

  return { fileMap, attachmentsDownloaded, attachmentWarnings };
}

function attachmentStatusForError(error) {
  if (error instanceof UserFacingError) return "access_denied";
  if (/\bHTTP 404\b/.test(error.message)) return "file_not_found";
  if (/\bHTTP 401\b|\bHTTP 403\b/.test(error.message)) return "access_denied";
  return "download_failed";
}

function chooseAttachmentFilename(
  attachmentsDir,
  ref,
  downloadFileName,
  contentType,
  attachmentRecords,
) {
  const sourceName = downloadFileName || ref.name || ref.fileId;
  const parsed = path.parse(sourceName);
  const base = slugify(parsed.name || ref.fileId);
  const extension =
    sanitizedExtension(parsed.ext) || extensionFromContentType(contentType) || "";
  const preferred = `${base}${extension}`;
  const usedNames = new Set(
    Object.values(attachmentRecords)
      .map((record) => record.relativePath?.split("/").pop())
      .filter(Boolean),
  );

  let candidate = preferred;
  let suffix = 2;
  while (usedNames.has(candidate) || existsSync(path.join(attachmentsDir, candidate))) {
    candidate = `${base}-${suffix}${extension}`;
    suffix += 1;
  }
  return candidate;
}

function sanitizedExtension(extension) {
  if (!extension) return "";
  const clean = extension.toLowerCase().replace(/[^.a-z0-9]/g, "");
  return clean.startsWith(".") ? clean.slice(0, 12) : "";
}

function extensionFromContentType(contentType) {
  const mime = contentType.split(";")[0].trim().toLowerCase();
  const map = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
  };
  return map[mime] || "";
}

function conversationToMarkdown(conversation, candidate, fileMap, messageIds = null) {
  const id = conversation.id || conversation.conversation_id || candidate.id;
  const title = conversation.title || candidate.title || "Untitled";
  const lines = [
    "---",
    `title: "${escapeYaml(title)}"`,
    `chatgpt_id: "${escapeYaml(id)}"`,
    `chatgpt_url: "https://chatgpt.com/c/${escapeYaml(id)}"`,
    `create_time: "${formatTimestamp(conversation.create_time || candidate.createTimeMs)}"`,
    `update_time: "${formatTimestamp(conversation.update_time || candidate.updateTimeMs)}"`,
  ];
  if (candidate.project) {
    lines.push(`project_id: "${escapeYaml(candidate.project.id)}"`);
    lines.push(`project_name: "${escapeYaml(candidate.project.name)}"`);
  }
  lines.push("---", "", `# ${title}`, "");
  lines.push(`[Open in ChatGPT](https://chatgpt.com/c/${id})`, "");
  const messageSections = messageSectionsToMarkdown(conversation, fileMap, messageIds);
  if (messageSections) lines.push(messageSections);

  return `${lines.join("\n").replace(/\n{4,}/g, "\n\n\n").trim()}\n`;
}

function messageSectionsToMarkdown(conversation, fileMap, messageIds = null) {
  const lines = [];
  const ids = messageIds ?? visibleMessageIds(conversation);
  for (const messageId of ids) {
    const message = conversation.mapping?.[messageId]?.message;
    if (!message || shouldSkipMessage(message)) continue;

    const role = message.author?.role || "unknown";
    const rendered = renderMessageContent(message, fileMap).trim();
    if (!rendered) continue;

    lines.push(`## ${roleLabel(role, message)}`);
    const messageTime = formatTimestamp(message.create_time);
    if (messageTime !== "unknown") {
      lines.push("", `_${messageTime}_`);
    }
    lines.push("", rendered, "");
  }

  return lines.join("\n").replace(/\n{4,}/g, "\n\n\n").trim();
}

function visibleMessageIds(conversation) {
  const mapping = conversation.mapping || {};
  if (conversation.current_node && mapping[conversation.current_node]) {
    const ids = [];
    let currentId = conversation.current_node;
    const seen = new Set();
    while (currentId && mapping[currentId] && !seen.has(currentId)) {
      seen.add(currentId);
      ids.push(currentId);
      currentId = mapping[currentId].parent;
    }
    return ids.reverse();
  }

  const rootId = Object.entries(mapping).find(([, node]) => node.parent == null)?.[0];
  const ids = [];
  let currentId = rootId;
  const seen = new Set();
  while (currentId && mapping[currentId] && !seen.has(currentId)) {
    seen.add(currentId);
    ids.push(currentId);
    currentId = mapping[currentId].children?.[0];
  }
  return ids;
}

function shouldSkipMessage(message) {
  if (message.metadata?.is_visually_hidden_from_conversation) return true;
  const role = message.author?.role || "";
  if (role === "system" || role === "tool") return true;
  if (role === "assistant" && message.recipient && message.recipient !== "all") {
    return true;
  }

  const contentType = message.content?.content_type || "";
  return (
    contentType === "model_editable_context" ||
    contentType === "thoughts" ||
    contentType === "reasoning_recap"
  );
}

function roleLabel(role, message) {
  if (message.metadata?.is_async_task_result_message) {
    return "Assistant (Deep Research Result)";
  }
  if (role === "user") return "User";
  if (role === "assistant") return "Assistant";
  if (role === "tool") return `Tool${message.author?.name ? `: ${message.author.name}` : ""}`;
  if (role === "system") return "System";
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function renderMessageContent(message, fileMap) {
  const content = message.content || {};
  const renderedParts = renderContentParts(content, fileMap);
  const attachmentLinks = renderAttachmentLinks(message, fileMap);
  const combined = [...renderedParts, ...attachmentLinks].filter(Boolean);
  if (combined.length > 0) return combined.join("\n\n");
  if (content.content_type) return `> [Unsupported content type: ${content.content_type}]`;
  return "";
}

function renderContentParts(content, fileMap) {
  if (content.content_type === "text" && Array.isArray(content.parts)) {
    return content.parts.filter((part) => typeof part === "string");
  }
  if (content.content_type === "multimodal_text" && Array.isArray(content.parts)) {
    return content.parts
      .map((part) => renderMultimodalPart(part, fileMap))
      .filter(Boolean);
  }
  if (content.content_type === "code" && content.text) {
    return [`\`\`\`\n${content.text}\n\`\`\``];
  }
  if (content.content_type === "tether_browsing_display") {
    const text = (content.parts || []).filter((part) => typeof part === "string").join("\n");
    return text ? [`> **Browsing result**\n>\n> ${text.replace(/\n/g, "\n> ")}`] : [];
  }
  if (Array.isArray(content.parts)) {
    const strings = content.parts.filter((part) => typeof part === "string");
    if (strings.length > 0) return strings;
  }
  if (typeof content.text === "string") return [content.text];
  return [];
}

function renderMultimodalPart(part, fileMap) {
  if (typeof part === "string") return part;
  if (!part || typeof part !== "object") return "";
  if (part.asset_pointer) {
    const fileId = normalizeAssetPointer(part.asset_pointer);
    const relativePath = fileMap[fileId];
    if (relativePath && part.content_type === "image_asset_pointer") {
      return `![image](${encodeURI(relativePath)})`;
    }
    if (relativePath) {
      return `[Attachment](${encodeURI(relativePath)})`;
    }
    return `[${part.content_type || "asset"}: ${fileId}]`;
  }
  return `\`${JSON.stringify(part)}\``;
}

function renderAttachmentLinks(message, fileMap) {
  const links = [];
  for (const attachment of message.metadata?.attachments || []) {
    const fileId = attachment.id || attachment.file_id;
    const relativePath = fileMap[fileId];
    if (relativePath) {
      links.push(`- [${attachment.name || "attachment"}](${encodeURI(relativePath)})`);
    } else if (fileId) {
      links.push(`- [Attachment unavailable: ${fileId}]`);
    }
  }
  return links.length > 0 ? [`Attachments:\n${links.join("\n")}`] : [];
}

function escapeYaml(value) {
  return String(value ?? "")
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\n/g, " ");
}

function formatTimestamp(value) {
  const ms = timestampToMs(value);
  return ms ? new Date(ms).toISOString() : "unknown";
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
