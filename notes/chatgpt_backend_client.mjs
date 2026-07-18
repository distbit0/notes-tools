import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

const REQUEST_RETRY_LIMIT = 3;
const REQUEST_TIMEOUT_MS = 60_000;
const RATE_LIMIT_STATE_VERSION = 1;
const RATE_LIMIT_COOLDOWN_MS = 24 * 60 * 60 * 1000;
const UNREAD_ASYNC_STATUS = 4;
const UNREAD_FIELDS = ["is_unread", "unread", "has_unread", "isUnread", "hasUnread"];
const STATUS_FIELDS = [
  "status",
  "conversation_status",
  "conversationStatus",
  "last_message_status",
  "lastMessageStatus",
];
const CUT_OFF_STATUSES = new Set([
  "aborted",
  "cancelled",
  "canceled",
  "cut_off",
  "error",
  "failed",
  "in_progress",
  "interrupted",
]);

export class UserFacingError extends Error {}
class NonRetryableRequestError extends Error {}

export class RateLimitCooldownError extends UserFacingError {
  constructor(blockedUntil) {
    super(`ChatGPT backend requests are paused until ${blockedUntil}.`);
    this.blockedUntil = blockedUntil;
  }
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
    [
      "run",
      "--project",
      options.projectRoot,
      "python",
      "-c",
      buildPythonCookieReader(options),
    ],
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

export class ChatGptClient {
  constructor(options) {
    this.options = options;
    this.throttler = new RequestThrottler(options);
    this.deviceId = randomUUID();
    this.accessToken = "";
    this.accountId = "";
    this.requestCount = 0;
  }

  async initialize() {
    await this.assertRequestsAllowed();
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

  async waitForBrowserOpen() {
    await this.assertRequestsAllowed();
    await this.throttler.wait();
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

  async patchBackendJson(pathname, body) {
    const response = await this.fetchWithRetry(
      `https://chatgpt.com${pathname.startsWith("/") ? "" : "/"}${pathname}`,
      {
        method: "PATCH",
        headers: this.backendHeaders("application/json"),
        body: JSON.stringify(body),
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

  async fetchInterpreterDownloadInfo(conversationId, messageId, sandboxPath) {
    const query = new URLSearchParams({
      message_id: messageId,
      sandbox_path: sandboxPath,
    });
    const response = await this.fetchWithRetry(
      `https://chatgpt.com/backend-api/conversation/${encodeURIComponent(
        conversationId,
      )}/interpreter/download?${query}`,
      {
        headers: this.backendHeaders("application/json"),
      },
      { authErrorIsFatal: false },
    );
    return response.json();
  }

  async fetchDownload(downloadUrl) {
    const response = await this.fetchWithRetry(
      downloadUrl,
      {
        headers: this.backendHeaders("*/*"),
      },
      { authErrorIsFatal: false },
    );
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

  async assertRequestsAllowed() {
    const cooldown = await activeRateLimitCooldown(this.options.rateLimitStatePath);
    if (cooldown) throw new RateLimitCooldownError(cooldown.blockedUntil);
  }

  async fetchWithRetry(url, init, { authErrorIsFatal = true } = {}) {
    let lastError = null;
    for (let attempt = 1; attempt <= REQUEST_RETRY_LIMIT; attempt += 1) {
      await this.assertRequestsAllowed();
      await this.throttler.wait();
      this.requestCount += 1;
      try {
        const response = await fetch(url, {
          ...init,
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        if (response.ok) return response;

        if (response.status === 429) {
          const rateLimitState = await recordRateLimit(
            this.options.rateLimitStatePath,
            url,
          );
          throw new UserFacingError(
            `ChatGPT returned HTTP 429; all backend jobs are paused until ${rateLimitState.blockedUntil}.`,
          );
        }

        if (authErrorIsFatal && (response.status === 401 || response.status === 403)) {
          throw new UserFacingError(
            `ChatGPT request failed with HTTP ${response.status}. ` +
              "Authentication or Cloudflare access is no longer valid.",
          );
        }

        if (response.status >= 500 && attempt < REQUEST_RETRY_LIMIT) {
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

export async function activeRateLimitCooldown(rateLimitStatePath, nowMs = Date.now()) {
  if (!rateLimitStatePath || !existsSync(rateLimitStatePath)) return null;
  const state = JSON.parse(await readFile(rateLimitStatePath, "utf8"));
  if (
    state.version !== RATE_LIMIT_STATE_VERSION ||
    typeof state.blockedUntil !== "string" ||
    !Number.isFinite(Date.parse(state.blockedUntil))
  ) {
    throw new UserFacingError(
      `ChatGPT rate-limit state is invalid: ${rateLimitStatePath}`,
    );
  }
  return Date.parse(state.blockedUntil) > nowMs ? state : null;
}

async function recordRateLimit(rateLimitStatePath, requestUrl) {
  const observedAtMs = Date.now();
  const state = {
    version: RATE_LIMIT_STATE_VERSION,
    observedAt: new Date(observedAtMs).toISOString(),
    blockedUntil: new Date(observedAtMs + RATE_LIMIT_COOLDOWN_MS).toISOString(),
    requestPath: redactedRequestPath(requestUrl),
  };
  await writeAtomicJson(rateLimitStatePath, state);
  return state;
}

export async function seedRateLimitCooldown(
  rateLimitStatePath,
  observedAt,
  requestPath,
) {
  const observedAtMs = Date.parse(observedAt);
  if (!Number.isFinite(observedAtMs)) {
    throw new UserFacingError(`Invalid ChatGPT rate-limit timestamp: ${observedAt}`);
  }
  const state = {
    version: RATE_LIMIT_STATE_VERSION,
    observedAt: new Date(observedAtMs).toISOString(),
    blockedUntil: new Date(observedAtMs + RATE_LIMIT_COOLDOWN_MS).toISOString(),
    requestPath,
  };
  await writeAtomicJson(rateLimitStatePath, state);
  return state;
}

async function writeAtomicJson(targetPath, value) {
  await mkdir(path.dirname(targetPath), { recursive: true });
  const temporaryPath = path.join(
    path.dirname(targetPath),
    `.${path.basename(targetPath)}.${process.pid}.${Date.now()}.tmp`,
  );
  await writeFile(temporaryPath, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await rename(temporaryPath, targetPath);
}

function redactedRequestPath(requestUrl) {
  try {
    const url = new URL(requestUrl);
    return `${url.pathname}${url.search}`;
  } catch {
    return "unparseable-request-url";
  }
}

export function pendingSignalFromListItem(item) {
  const unread = unreadFlag(item);
  return {
    unreadKnown: unread.known,
    unread: unread.value,
    cutOff:
      isCutOffStatus(statusValue(item)) ||
      (item?.mapping ? detailCutOffReason(item) : false),
  };
}

export function pendingRecordsFromCandidate(candidate, conversation) {
  const latest = latestMessage(conversation);
  const conversationId =
    conversation?.conversation_id || conversation?.id || candidate.id;
  const title = candidate.title || conversation?.title;
  const records = [];
  if (candidate.pendingSignal.unread) {
    records.push({
      conversationId,
      reason: "unread",
      title,
      latestMessageId: latest?.id || "",
    });
  }
  if (candidate.pendingSignal.cutOff || detailCutOffReason(conversation)) {
    records.push({
      conversationId,
      reason: "cut_off",
      title,
      latestMessageId: latest?.id || "",
    });
  }
  return records;
}

function unreadFlag(item) {
  for (const field of UNREAD_FIELDS) {
    const value = item?.[field];
    if (typeof value === "boolean") return { known: true, value };
  }
  for (const field of ["async_status", "asyncStatus"]) {
    if (Object.prototype.hasOwnProperty.call(item || {}, field)) {
      return { known: true, value: Number(item[field]) === UNREAD_ASYNC_STATUS };
    }
  }
  return { known: false, value: false };
}

function statusValue(object) {
  for (const field of STATUS_FIELDS) {
    const value = object?.[field];
    if (typeof value === "string" && value.trim() !== "") return value.trim();
  }
  return "";
}

function isCutOffStatus(value) {
  return CUT_OFF_STATUSES.has(String(value || "").trim().toLowerCase());
}

function detailCutOffReason(conversation) {
  const latest = latestMessage(conversation);
  if (!latest || latest?.author?.role !== "assistant") return false;
  return isCutOffStatus(statusValue(latest) || latest?.status);
}

function latestMessage(conversation) {
  const messages = Object.values(conversation?.mapping || {})
    .map((node) => node?.message)
    .filter((message) => message && typeof message === "object");
  return messages.reduce((latest, message) => {
    const latestTime = Number(latest?.create_time || 0);
    const messageTime = Number(message?.create_time || 0);
    return messageTime >= latestTime ? message : latest;
  }, null);
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
