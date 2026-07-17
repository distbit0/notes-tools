#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
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

import {
  activeRateLimitCooldown,
  ChatGptClient,
  pendingRecordsFromCandidate,
  pendingSignalFromListItem,
  UserFacingError,
} from "./chatgpt_backend_client.mjs";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_PATH);
const MISC_ROOT = path.dirname(SCRIPT_DIR);
const HOME_DIR = os.homedir();
const CONFIG_PATH = path.join(SCRIPT_DIR, "config.json");
const PENDING_CONVERSATIONS_SCRIPT_PATH = path.join(
  SCRIPT_DIR,
  "chatgpt_pending_convos_to_notes.py",
);
const DEFAULT_OUTPUT_ROOT = path.join(HOME_DIR, "notes/chatgpt-conversations");
const DEFAULT_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/chatgpt-convos-to-notes/state.json",
);
const DEFAULT_BROWSER_STATE_PATH = path.join(
  HOME_DIR,
  ".local/state/chatgpt-browser-actions/state.json",
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
const MARKDOWN_FORMAT_VERSION = 3;
const CONVERSATIONS_PAGE_SIZE = 100;
const PROJECT_CONVERSATIONS_PAGE_SIZE = 20;
const INTERACTIVE_HTML_LINK_PATTERN =
  /\bsandbox:\/{1,2}[^\s)\]>"']+[.]html(?:[?#][^\s)\]>"']*)?/i;
const ARCHIVED_HTML_HINT_PATTERN = /[.]html(?:[?#)\]\s]|$)|\btext\/html\b/i;
const PERMANENT_ATTACHMENT_STATUSES = new Set([
  "downloaded",
  "file_not_found",
  "access_denied",
]);

function usage() {
  return `Usage: chatgpt_convos_to_notes.mjs [options]

Archive active ChatGPT conversations newer than the cutoff into ~/notes, or run
the independent browser actions.

Options:
  --output <dir>             Output root (default: ${DEFAULT_OUTPUT_ROOT})
  --state <file>             Archive ledger (default: ${DEFAULT_STATE_PATH})
  --browser-actions          Drain the browser queue and open interactive HTML
  --browser-state <file>     Browser-actions ledger
                             (default: ${DEFAULT_BROWSER_STATE_PATH})
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
  const browserQueue = configuration.chatgptConversationSync;
  if (
    !browserQueue ||
    typeof browserQueue.openInBrowserProject !== "string" ||
    !browserQueue.openInBrowserProject.trim() ||
    typeof browserQueue.braveExecutable !== "string" ||
    !browserQueue.braveExecutable.trim()
  ) {
    throw new UserFacingError(
      "notes/config.json must define chatgptConversationSync.openInBrowserProject and braveExecutable.",
    );
  }
  return browserQueue;
}

function parseArgs(argv, browserQueue = loadConfiguration()) {
  const options = {
    outputRoot: DEFAULT_OUTPUT_ROOT,
    statePath: DEFAULT_STATE_PATH,
    browserStatePath: DEFAULT_BROWSER_STATE_PATH,
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
    browserActionsOnly: false,
    bearer: process.env.CHATGPT_BEARER_TOKEN || "",
    openInBrowserProject: browserQueue.openInBrowserProject,
    braveExecutable: browserQueue.braveExecutable,
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
    } else if (arg === "--browser-actions") {
      options.browserActionsOnly = true;
    } else if (arg === "--browser-state") {
      options.browserStatePath = path.resolve(value());
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
  if (options.browserActionsOnly && (options.statusOnly || options.forceRun)) {
    throw new UserFacingError(
      "--browser-actions cannot be combined with archive-only --status or --force-run.",
    );
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

function defaultBrowserActionState(archiveState) {
  const conversations = {};
  for (const [conversationId, archiveRecord] of Object.entries(
    archiveState.conversations,
  )) {
    if (
      archiveRecord.interactiveHtmlCheckedUpdateTimeMs === undefined &&
      !archiveRecord.interactiveHtmlOpenedAt
    ) {
      continue;
    }
    conversations[conversationId] = {
      interactiveHtmlCheckedUpdateTimeMs:
        archiveRecord.interactiveHtmlCheckedUpdateTimeMs,
      ...(archiveRecord.interactiveHtmlOpenedAt
        ? { interactiveHtmlOpenedAt: archiveRecord.interactiveHtmlOpenedAt }
        : {}),
    };
  }
  return {
    version: 3,
    conversations,
    scanWatermarks: { normal: null, projects: {} },
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

async function loadBrowserActionState(browserStatePath, archiveState) {
  if (!existsSync(browserStatePath)) {
    const migratedState = defaultBrowserActionState(archiveState);
    const migratedCount = Object.keys(migratedState.conversations).length;
    if (migratedCount > 0) {
      console.log(
        `Migrated interactive HTML history for ${migratedCount} conversation(s) from the archive ledger.`,
      );
    }
    return migratedState;
  }

  const state = JSON.parse(await readFile(browserStatePath, "utf8"));
  return {
    version: 3,
    conversations:
      state.conversations && typeof state.conversations === "object"
        ? state.conversations
        : {},
    scanWatermarks: normalizeBrowserScanWatermarks(state.scanWatermarks),
  };
}

function normalizeBrowserScanWatermarks(scanWatermarks) {
  if (scanWatermarks === undefined) return { normal: null, projects: {} };
  if (!scanWatermarks || typeof scanWatermarks !== "object") {
    throw new Error("Browser scan watermarks must be an object.");
  }
  const normal = scanWatermarks.normal ?? null;
  if (normal !== null && !Number.isFinite(normal)) {
    throw new Error("Normal-conversation scan watermark must be a timestamp.");
  }
  const projects = scanWatermarks.projects ?? {};
  if (!projects || typeof projects !== "object") {
    throw new Error("Project scan watermarks must be an object.");
  }
  for (const [projectId, updateTimeMs] of Object.entries(projects)) {
    if (!Number.isFinite(updateTimeMs)) {
      throw new Error(`Project scan watermark ${projectId} must be a timestamp.`);
    }
  }
  return { normal, projects };
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
    const { candidates } = await collectCandidates(client, options, projects);
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

async function runBrowserActions(options) {
  const archiveState = await loadState(options.statePath);
  const browserState = await loadBrowserActionState(
    options.browserStatePath,
    archiveState,
  );
  await saveState(options.browserStatePath, browserState);

  const summary = {
    discovered: 0,
    interactiveHtmlConversationsChecked: 0,
    interactiveHtmlTabsOpened: 0,
    browserQueueTabsOpened: 0,
    projectConversationsRemoved: 0,
    pendingConversationReminders: 0,
    apiRequests: 0,
  };
  const cooldown = await activeRateLimitCooldown(options.rateLimitStatePath);
  if (cooldown) {
    summary.pendingConversationReminders = consumePendingRecords([]);
    return cooldownResult(cooldown, summary);
  }

  const client = new ChatGptClient(options);
  await client.initialize();

  const projects = await fetchProjects(client);
  const { candidates, scanWatermarks } = await collectCandidates(
    client,
    options,
    projects,
    browserState.scanWatermarks,
  );
  summary.discovered = candidates.length;
  const openedThisRun = new Set();
  const pendingRecords = [];
  if (
    candidates.length > 0 &&
    !candidates.some((candidate) => candidate.pendingSignal.unreadKnown)
  ) {
    console.warn(
      "Warning: changed ChatGPT conversations had no recognized unread field; pending reminders cannot infer unread state.",
    );
  }

  for (const candidate of candidates) {
    const browserRecord = browserState.conversations[candidate.id] || {};
    const legacyOpenMigrationNeeded = Boolean(
      browserRecord.interactiveHtmlOpenedAt,
    );
    const interactiveHtmlCheckNeeded = needsInteractiveHtmlCheck(
      browserRecord,
      candidate,
    );
    const pendingCheckNeeded =
      candidate.pendingSignal.unread || candidate.pendingSignal.cutOff;
    if (
      !legacyOpenMigrationNeeded &&
      !interactiveHtmlCheckNeeded &&
      !pendingCheckNeeded
    ) {
      continue;
    }

    let conversation = null;
    const archiveIsCurrent = isConversationCurrent(archiveState, candidate);
    const browserConversationNeeded =
      legacyOpenMigrationNeeded ||
      (interactiveHtmlCheckNeeded &&
        (!archiveIsCurrent ||
          (await archiveMayContainInteractiveHtml(
            options.outputRoot,
            archiveState.conversations[candidate.id],
          ))));
    if (pendingCheckNeeded || browserConversationNeeded) {
      conversation = await client.fetchBackendJson(
        `/backend-api/conversation/${encodeURIComponent(candidate.id)}`,
      );
    }

    if (pendingCheckNeeded) {
      pendingRecords.push(...pendingRecordsFromCandidate(candidate, conversation));
    }

    if (legacyOpenMigrationNeeded) {
      migrateLegacyInteractiveHtmlOpen(browserRecord, conversation);
      console.log(`Migrated interactive HTML open: ${candidate.title}`);
    }

    const interactiveHtmlMessage = conversation
      ? lastAssistantInteractiveHtmlMessage(conversation)
      : null;
    const openedMessages = browserRecord.interactiveHtmlOpenedMessages || {};
    if (interactiveHtmlMessage && !openedMessages[interactiveHtmlMessage.id]) {
      await client.waitForBrowserOpen();
      openBraveTab(
        options,
        `https://chatgpt.com/c/${encodeURIComponent(candidate.id)}`,
      );
      openedMessages[interactiveHtmlMessage.id] = new Date().toISOString();
      browserRecord.interactiveHtmlOpenedMessages = openedMessages;
      summary.interactiveHtmlTabsOpened += 1;
      openedThisRun.add(candidate.id);
      console.log(`Opened interactive HTML conversation: ${candidate.title}`);
    }
    if (legacyOpenMigrationNeeded || interactiveHtmlCheckNeeded) {
      browserRecord.interactiveHtmlCheckedUpdateTimeMs = candidate.updateTimeMs;
      browserState.conversations[candidate.id] = browserRecord;
      summary.interactiveHtmlConversationsChecked += 1;
      await saveState(options.browserStatePath, browserState);
    }
  }

  summary.pendingConversationReminders = consumePendingRecords(pendingRecords);
  const browserQueueResult = await openAndRemoveProjectConversations(
    client,
    options,
    projects,
    openedThisRun,
  );
  summary.browserQueueTabsOpened = browserQueueResult.opened;
  summary.projectConversationsRemoved = browserQueueResult.removed;
  summary.apiRequests = client.requestCount;
  if (options.maxConversations === null) {
    browserState.scanWatermarks = scanWatermarks;
    await saveState(options.browserStatePath, browserState);
  }
  return { status: "success", summary };
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

function consumePendingRecords(records) {
  const result = spawnSync(
    "uv",
    [
      "run",
      "--project",
      MISC_ROOT,
      "python",
      PENDING_CONVERSATIONS_SCRIPT_PATH,
    ],
    {
      input: JSON.stringify(records),
      encoding: "utf8",
      maxBuffer: 4 * 1024 * 1024,
    },
  );
  if (result.status !== 0) {
    const error = (result.stderr || result.stdout || "").trim();
    throw new UserFacingError(
      `Could not persist pending ChatGPT reminders: ${error || "unknown error"}`,
    );
  }
  if (result.stderr.trim()) console.warn(result.stderr.trim());

  const outputLines = result.stdout.trim().split(/\r?\n/);
  const summaryLine =
    outputLines.length > 0 ? outputLines.at(-1) : result.stdout.trim();
  try {
    const summary = JSON.parse(summaryLine);
    if (!Number.isInteger(summary.appended)) throw new Error("missing appended count");
    return summary.appended;
  } catch (error) {
    throw new UserFacingError(
      `Pending ChatGPT reminder writer returned invalid output: ${error.message}`,
    );
  }
}

async function collectCandidates(
  client,
  options,
  projects,
  previousScanWatermarks = null,
) {
  const candidatesById = new Map();
  const normalMinimumUpdateTimeMs = Math.max(
    options.cutoffMs,
    previousScanWatermarks?.normal ?? options.cutoffMs,
  );
  const latestNormalUpdateTimeMs = await collectNormalCandidates(
    client,
    options,
    candidatesById,
    normalMinimumUpdateTimeMs,
  );
  const scanWatermarks = {
    normal:
      latestNormalUpdateTimeMs > 0
        ? Math.max(previousScanWatermarks?.normal ?? 0, latestNormalUpdateTimeMs)
        : previousScanWatermarks?.normal ?? null,
    projects: { ...(previousScanWatermarks?.projects || {}) },
  };

  if (!hasReachedCandidateLimit(options, candidatesById)) {
    for (const project of projects) {
      if (hasReachedCandidateLimit(options, candidatesById)) break;
      const previousProjectWatermark =
        previousScanWatermarks?.projects[project.id] ?? null;
      const latestProjectUpdateTimeMs = await collectSingleProjectCandidates(
        client,
        options,
        candidatesById,
        project,
        Math.max(
          options.cutoffMs,
          previousProjectWatermark ?? options.cutoffMs,
        ),
      );
      if (latestProjectUpdateTimeMs > 0) {
        scanWatermarks.projects[project.id] = Math.max(
          previousProjectWatermark ?? 0,
          latestProjectUpdateTimeMs,
        );
      }
    }
  }

  return {
    candidates: [...candidatesById.values()]
      .sort((left, right) => right.updateTimeMs - left.updateTimeMs)
      .slice(0, options.maxConversations ?? undefined),
    scanWatermarks,
  };
}

async function collectNormalCandidates(
  client,
  options,
  candidatesById,
  minimumUpdateTimeMs,
) {
  let offset = 0;
  let latestUpdateTimeMs = 0;
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
    const page = addCandidatePage(
      items,
      null,
      minimumUpdateTimeMs,
      candidatesById,
    );
    latestUpdateTimeMs = Math.max(latestUpdateTimeMs, page.latestUpdateTimeMs);
    const reachedOlderPage = page.oldestUpdateTimeMs < minimumUpdateTimeMs;
    if (reachedOlderPage || items.length < CONVERSATIONS_PAGE_SIZE) break;
    offset += items.length;
  }
  return latestUpdateTimeMs;
}

async function fetchProjects(client) {
  const projects = [];
  let cursor = null;
  do {
    const query = new URLSearchParams({
      owned_only: "true",
      conversations_per_gizmo: String(PROJECT_CONVERSATIONS_PAGE_SIZE),
    });
    if (cursor) query.set("cursor", cursor);
    const data = await client.fetchBackendJson(
      `/backend-api/gizmos/snorlax/sidebar?${query.toString()}`,
    );
    const items = Array.isArray(data.items) ? data.items : [];
    for (const item of items) {
      const gizmo = item.gizmo?.gizmo || item.gizmo;
      if (!gizmo?.id) continue;
      if (!item.conversations || !Array.isArray(item.conversations.items)) {
        throw new Error(`ChatGPT project ${gizmo.id} is missing conversations.`);
      }
      projects.push({
        id: gizmo.id,
        name: gizmo.display?.name || "Untitled Project",
        embeddedConversations: item.conversations.items,
        hasMoreConversations: Boolean(item.conversations.cursor),
      });
    }
    cursor = data.cursor || null;
  } while (cursor);
  return projects;
}

async function collectSingleProjectCandidates(
  client,
  options,
  candidatesById,
  project,
  minimumUpdateTimeMs,
) {
  const embeddedPage = addCandidatePage(
    project.embeddedConversations,
    project,
    minimumUpdateTimeMs,
    candidatesById,
  );
  let latestUpdateTimeMs = embeddedPage.latestUpdateTimeMs;
  if (
    !project.hasMoreConversations ||
    (embeddedPage.itemCount > 0 &&
      embeddedPage.oldestUpdateTimeMs < minimumUpdateTimeMs) ||
    hasReachedCandidateLimit(options, candidatesById)
  ) {
    return latestUpdateTimeMs;
  }

  let cursor = "0";
  while (cursor && !hasReachedCandidateLimit(options, candidatesById)) {
    const data = await client.fetchBackendJson(
      `/backend-api/gizmos/${encodeURIComponent(
        project.id,
      )}/conversations?cursor=${encodeURIComponent(cursor)}`,
    );
    const items = Array.isArray(data.items) ? data.items : [];
    if (items.length === 0) break;
    const page = addCandidatePage(
      items,
      project,
      minimumUpdateTimeMs,
      candidatesById,
    );
    latestUpdateTimeMs = Math.max(latestUpdateTimeMs, page.latestUpdateTimeMs);
    const reachedOlderPage = page.oldestUpdateTimeMs < minimumUpdateTimeMs;
    if (reachedOlderPage) break;
    cursor = data.cursor || null;
  }
  return latestUpdateTimeMs;
}

function addCandidatePage(
  items,
  project,
  minimumUpdateTimeMs,
  candidatesById,
) {
  const candidates = items.map((item) =>
    normalizeConversationListItem(item, project),
  );
  for (let index = 1; index < candidates.length; index += 1) {
    if (candidates[index - 1].updateTimeMs < candidates[index].updateTimeMs) {
      throw new Error("ChatGPT conversation page is not ordered by update time.");
    }
  }
  for (const candidate of candidates) {
    if (candidate.updateTimeMs >= minimumUpdateTimeMs) {
      candidatesById.set(candidate.id, candidate);
    }
  }
  return {
    itemCount: candidates.length,
    latestUpdateTimeMs: candidates[0]?.updateTimeMs ?? 0,
    oldestUpdateTimeMs: candidates.at(-1)?.updateTimeMs ?? 0,
  };
}

async function fetchAllProjectConversations(client, project) {
  if (!project.hasMoreConversations) return project.embeddedConversations;
  return fetchProjectConversationsFromApi(client, project);
}

async function fetchProjectConversationsFromApi(client, project) {
  const conversations = [];
  let cursor = "0";
  while (cursor) {
    const data = await client.fetchBackendJson(
      `/backend-api/gizmos/${encodeURIComponent(
        project.id,
      )}/conversations?cursor=${encodeURIComponent(cursor)}`,
    );
    const items = Array.isArray(data.items) ? data.items : [];
    conversations.push(...items);
    cursor = data.cursor || null;
  }
  return conversations;
}

async function openAndRemoveProjectConversations(
  client,
  options,
  projects,
  alreadyOpenedConversationIds = new Set(),
) {
  const matchingProjects = projects.filter(
    (project) => project.name === options.openInBrowserProject,
  );
  if (matchingProjects.length === 0) {
    console.warn(
      `Warning: ChatGPT project ${JSON.stringify(
        options.openInBrowserProject,
      )} was not found; no browser tabs were opened.`,
    );
    return { opened: 0, removed: 0 };
  }
  if (matchingProjects.length > 1) {
    throw new UserFacingError(
      `Multiple ChatGPT projects are named ${JSON.stringify(
        options.openInBrowserProject,
      )}; refusing to choose one.`,
    );
  }

  const browserProject = matchingProjects[0];
  const conversations = (await fetchAllProjectConversations(
    client,
    browserProject,
  )).map((conversation) => {
    const id = conversation.id || conversation.conversation_id;
    if (!id) {
      throw new Error(
        `${options.openInBrowserProject} conversation is missing its id.`,
      );
    }
    return { id, title: conversation.title || id };
  });
  if (conversations.length === 0) return { opened: 0, removed: 0 };

  let opened = 0;
  for (const conversation of conversations) {
    if (alreadyOpenedConversationIds.has(conversation.id)) continue;
    const conversationUrl = `https://chatgpt.com/c/${encodeURIComponent(
      conversation.id,
    )}`;
    await client.waitForBrowserOpen();
    openBraveTab(options, conversationUrl);
    opened += 1;
  }

  let removed = 0;
  for (const conversation of conversations) {
    const response = await client.patchBackendJson(
      `/backend-api/conversation/${encodeURIComponent(conversation.id)}`,
      { gizmo_id: "" },
    );
    if (response.success !== true) {
      throw new Error(
        `ChatGPT did not confirm removal of conversation ${conversation.id} from ${options.openInBrowserProject}.`,
      );
    }
    removed += 1;
    console.log(
      `Opened and removed from ${options.openInBrowserProject}: ${conversation.title}`,
    );
  }

  const originalConversationIds = new Set(conversations.map(({ id }) => id));
  const remainingConversationIds = new Set(
    (await fetchProjectConversationsFromApi(client, browserProject)).map(
      (conversation) => conversation.id || conversation.conversation_id,
    ),
  );
  const failedRemovals = [...originalConversationIds].filter(
    (conversationId) => remainingConversationIds.has(conversationId),
  );
  if (failedRemovals.length > 0) {
    throw new Error(
      `ChatGPT left ${failedRemovals.length} opened conversation(s) in ${options.openInBrowserProject}.`,
    );
  }
  return { opened, removed };
}

function openBraveTab(options, conversationUrl) {
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
    pendingSignal: pendingSignalFromListItem(item),
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

function needsInteractiveHtmlCheck(record, candidate) {
  return record?.interactiveHtmlCheckedUpdateTimeMs !== candidate.updateTimeMs;
}

async function archiveMayContainInteractiveHtml(outputRoot, record) {
  if (!record?.folderName) return true;
  const markdownPath = path.join(outputRoot, record.folderName, "conversation.md");
  if (!existsSync(markdownPath)) return true;
  const markdown = await readFile(markdownPath, "utf8");
  return (
    INTERACTIVE_HTML_LINK_PATTERN.test(markdown) ||
    ARCHIVED_HTML_HINT_PATTERN.test(markdown)
  );
}

function lastAssistantInteractiveHtmlMessage(conversation) {
  const latestAssistantMessage = visibleAssistantMessages(conversation).at(-1);
  if (
    !latestAssistantMessage ||
    !contentContainsInteractiveHtml(latestAssistantMessage.message.content)
  ) {
    return null;
  }
  return {
    id: latestAssistantMessage.id,
    createdAtMs: timestampToMs(latestAssistantMessage.message.create_time),
  };
}

function visibleAssistantMessages(conversation) {
  const mapping = conversation.mapping || {};
  return visibleMessageIds(conversation)
    .map((messageId) => ({
      id: mapping[messageId]?.message?.id || messageId,
      message: mapping[messageId]?.message,
    }))
    .filter(
      ({ message }) =>
        message?.author?.role === "assistant" && !shouldSkipMessage(message),
    );
}

function migrateLegacyInteractiveHtmlOpen(record, conversation) {
  const openedAtMs = Date.parse(record.interactiveHtmlOpenedAt);
  if (!Number.isFinite(openedAtMs)) {
    throw new Error("Legacy interactive HTML open has an invalid timestamp.");
  }

  const previouslyOpenedMessage = visibleAssistantMessages(conversation)
    .map(({ id, message }) => ({
      id,
      message,
      createdAtMs: timestampToMs(message.create_time),
    }))
    .filter(
      ({ message, createdAtMs }) =>
        contentContainsInteractiveHtml(message.content) &&
        createdAtMs > 0 &&
        createdAtMs <= openedAtMs,
    )
    .sort((left, right) => right.createdAtMs - left.createdAtMs)[0];
  if (!previouslyOpenedMessage) {
    throw new Error(
      "Could not identify the assistant message for a legacy interactive HTML open.",
    );
  }

  record.interactiveHtmlOpenedMessages = {
    ...(record.interactiveHtmlOpenedMessages || {}),
    [previouslyOpenedMessage.id]: record.interactiveHtmlOpenedAt,
  };
  delete record.interactiveHtmlOpenedAt;
}

function contentContainsInteractiveHtml(value, fieldName = "") {
  if (typeof value === "string") {
    return (
      INTERACTIVE_HTML_LINK_PATTERN.test(value) ||
      ((fieldName === "content_type" || fieldName === "mime_type") &&
        value.toLowerCase() === "text/html")
    );
  }
  if (Array.isArray(value)) {
    return value.some((item) => contentContainsInteractiveHtml(item));
  }
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(([key, item]) =>
    contentContainsInteractiveHtml(item, key),
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

  const result = options.browserActionsOnly
    ? await runBrowserActions(options)
    : await syncChatGptConversations(options);
  if (result.summary) {
    if (options.browserActionsOnly) {
      console.log(
        [
          `Discovered: ${result.summary.discovered}`,
          `Interactive HTML conversations checked: ${result.summary.interactiveHtmlConversationsChecked}`,
          `Interactive HTML tabs opened: ${result.summary.interactiveHtmlTabsOpened}`,
          `Browser queue tabs opened: ${result.summary.browserQueueTabsOpened}`,
          `Removed from browser queue: ${result.summary.projectConversationsRemoved}`,
          `Pending conversation reminders: ${result.summary.pendingConversationReminders}`,
          `API requests: ${result.summary.apiRequests}`,
          ...(result.summary.rateLimitedUntil
            ? [`Rate limited until: ${result.summary.rateLimitedUntil}`]
            : []),
        ].join("\n"),
      );
      return;
    }
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
  lastAssistantInteractiveHtmlMessage,
  messageIdsToPersist,
  openAndRemoveProjectConversations,
  parseArgs,
  runBrowserActions,
  syncChatGptConversations,
  timestampToMs,
};
