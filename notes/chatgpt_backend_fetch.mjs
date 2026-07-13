#!/usr/bin/env node

import { pathToFileURL } from "node:url";

const CHATGPT_BASE = "https://chatgpt.com";
const BROWSER_USER_AGENT =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36";
const CONVERSATION_SCAN_LIMIT = 100;
const REQUEST_TIMEOUT_MS = 15000;
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

async function readJsonStdin() {
  let text = "";
  for await (const chunk of process.stdin) {
    text += chunk;
  }
  return JSON.parse(text);
}

function assertString(value, name) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${name} is empty`);
  }
  return value;
}

async function fetchJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) {
      throw new Error(`${url} returned HTTP ${response.status}`);
    }
    return response.json();
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`${url} timed out after ${REQUEST_TIMEOUT_MS}ms`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function browserHeaders(cookieHeader) {
  return {
    accept: "application/json",
    cookie: cookieHeader,
    origin: CHATGPT_BASE,
    referer: `${CHATGPT_BASE}/`,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": BROWSER_USER_AGENT,
  };
}

function extractAccessToken(session) {
  const token = session?.accessToken ?? session?.access_token;
  if (typeof token === "string" && token.trim() !== "") {
    return token;
  }
  const keys = Object.keys(session || {}).join(", ");
  throw new Error(`ChatGPT auth session did not include an access token; keys: ${keys}`);
}

async function getAccessToken(cookieHeader) {
  const session = await fetchJson(`${CHATGPT_BASE}/api/auth/session`, {
    headers: browserHeaders(cookieHeader),
  });
  return extractAccessToken(session);
}

function conversationListItems(payload) {
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload?.conversations)) return payload.conversations;
  throw new Error("ChatGPT conversation list lacks items/conversations array");
}

function unreadFlag(item) {
  for (const field of UNREAD_FIELDS) {
    const value = item?.[field];
    if (typeof value === "boolean") {
      return { known: true, value };
    }
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
    if (typeof value === "string" && value.trim() !== "") {
      return value.trim();
    }
  }
  return "";
}

function isCutOffStatus(value) {
  return CUT_OFF_STATUSES.has(String(value || "").trim().toLowerCase());
}

function messageRole(message) {
  return message?.author?.role || "";
}

function mappingMessages(conversation) {
  const mapping = conversation?.mapping;
  if (!mapping || typeof mapping !== "object") return [];
  return Object.values(mapping)
    .map((node) => node?.message)
    .filter((message) => message && typeof message === "object");
}

function latestMessage(conversation) {
  const messages = mappingMessages(conversation);
  return messages.reduce((latest, message) => {
    const latestTime = Number(latest?.create_time || 0);
    const messageTime = Number(message?.create_time || 0);
    return messageTime >= latestTime ? message : latest;
  }, null);
}

function detailCutOffReason(conversation) {
  const latest = latestMessage(conversation);
  if (!latest || messageRole(latest) !== "assistant") return false;
  return isCutOffStatus(statusValue(latest) || latest?.status);
}

async function fetchConversationDetail(item, headers) {
  const conversationId = assertString(item.id || item.conversation_id, "conversation id");
  const path = `${CHATGPT_BASE}/backend-api/conversation/${conversationId}`;
  return fetchJson(path, { headers });
}

async function fetchConversationList(headers) {
  const pageLimit = 100;
  let offset = 0;
  const items = [];
  let sawUnreadField = false;
  let total = 0;

  while (items.length < CONVERSATION_SCAN_LIMIT) {
    const limit = Math.min(pageLimit, CONVERSATION_SCAN_LIMIT - items.length);
    const payload = await fetchJson(
      `${CHATGPT_BASE}/backend-api/conversations?offset=${offset}&limit=${limit}&order=updated`,
      { headers },
    );
    const pageItems = conversationListItems(payload);
    total = Number(payload?.total || total || 0);
    for (const item of pageItems) {
      if (unreadFlag(item).known) sawUnreadField = true;
    }
    items.push(...pageItems);

    offset += pageItems.length;
    if (pageItems.length < limit) break;
    if (total > 0 && offset >= total) break;
  }

  if (items.length > 0 && !sawUnreadField) {
    process.stderr.write(
      "WARNING: ChatGPT conversation list had no recognized unread field; unread ChatGPT conversations cannot be inferred from backend state\n",
    );
  }
  if (total > items.length) {
    process.stderr.write(
      `WARNING: scanned ${items.length} of ${total} ChatGPT conversations; older conversations were not inspected\n`,
    );
  }
  return items;
}

function outputRecord(item, conversation, reason) {
  const conversationId = assertString(
    conversation?.conversation_id || item.id || item.conversation_id,
    "conversation id",
  );
  const title = assertString(
    item.title || conversation?.title,
    `conversation title for ${conversationId}`,
  );
  const latest = latestMessage(conversation);
  return {
    conversationId,
    reason,
    title,
    latestMessageId: latest?.id || "",
  };
}

export function recordsFromFixture(fixture) {
  const listPayload = fixture?.listPayload ?? { items: fixture?.items ?? [] };
  const items = conversationListItems(listPayload);
  if (items.length > 0 && !items.some((item) => unreadFlag(item).known)) {
    throw new Error("ChatGPT conversation list had no recognized unread field");
  }

  const conversationsById = fixture?.conversationsById ?? {};
  const records = [];
  for (const item of items) {
    const unread = unreadFlag(item);
    const cutOffFromList = isCutOffStatus(statusValue(item));
    if (!unread.value && !cutOffFromList) continue;

    const conversationId = assertString(
      item.id || item.conversation_id,
      "conversation id",
    );
    const conversation = conversationsById[conversationId];
    if (!conversation) {
      throw new Error(`fixture lacks conversation detail for ${conversationId}`);
    }
    const cutOffFromDetail = detailCutOffReason(conversation);
    if (unread.value) {
      records.push(outputRecord(item, conversation, "unread"));
    }
    if (cutOffFromList || cutOffFromDetail) {
      records.push(outputRecord(item, conversation, "cut_off"));
    }
  }
  return records;
}

async function main() {
  const { cookieHeader } = await readJsonStdin();
  const cookie = assertString(cookieHeader, "cookieHeader");
  const accessToken = await getAccessToken(cookie);
  const headers = {
    ...browserHeaders(cookie),
    authorization: `Bearer ${accessToken}`,
  };

  const items = await fetchConversationList(headers);
  const records = [];
  for (const item of items) {
    const unread = unreadFlag(item);
    const cutOffFromItem =
      isCutOffStatus(statusValue(item)) || detailCutOffReason(item);
    if (!unread.value && !cutOffFromItem) continue;

    const conversation = item.mapping ? item : await fetchConversationDetail(item, headers);
    const cutOffFromDetail = detailCutOffReason(conversation);
    if (unread.value) {
      records.push(outputRecord(item, conversation, "unread"));
    }
    if (cutOffFromItem || cutOffFromDetail) {
      records.push(outputRecord(item, conversation, "cut_off"));
    }
  }

  process.stdout.write(`${JSON.stringify(records)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exit(1);
  });
}
