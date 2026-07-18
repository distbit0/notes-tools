import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

export function interactiveHtmlMessage(messageId, message) {
  if (!contentContainsInteractiveHtml(message.content)) return null;
  return {
    id: messageId,
    createdAtMs: timestampToMs(message.create_time),
    artifacts: interactiveHtmlArtifacts(message.content),
  };
}

export function interactiveHtmlMessageById(conversation, messageId) {
  const message = Object.values(conversation.mapping || {}).find(
    (node) => node.message?.id === messageId,
  )?.message;
  if (message?.author?.role !== "assistant") return null;
  return interactiveHtmlMessage(messageId, message);
}

export function contentContainsInteractiveHtml(value, fieldName = "") {
  if (typeof value === "string") {
    return (
      /\bsandbox:\/{1,2}[^\s)\]>"']+[.]html(?:[?#][^\s)\]>"']*)?/i.test(
        value,
      ) ||
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

export async function completePendingInteractiveHtmlArtifacts({
  client,
  options,
  browserState,
  archiveState,
  summary,
  saveState,
  openBraveTab,
}) {
  for (const [conversationId, browserRecord] of Object.entries(
    browserState.conversations,
  )) {
    const pendingMessageIds = Object.keys(
      browserRecord.interactiveHtmlOpenedMessages || {},
    ).filter(
      (messageId) =>
        !interactiveHtmlArtifactsComplete(browserRecord, messageId),
    );
    if (pendingMessageIds.length === 0) continue;

    let conversation = null;
    for (const messageId of pendingMessageIds) {
      let message = interactiveHtmlMessageFromState(browserRecord, messageId);
      if (!message) {
        conversation ||= await client.fetchBackendJson(
          `/backend-api/conversation/${encodeURIComponent(conversationId)}`,
        );
        message = interactiveHtmlMessageById(conversation, messageId);
        if (!message) {
          throw new Error(
            `Could not recover interactive HTML message ${messageId} in conversation ${conversationId}.`,
          );
        }
      }
      await completeInteractiveHtmlActions({
        client,
        options,
        browserState,
        browserRecord,
        conversationId,
        conversationTitle:
          conversation?.title ||
          archiveState.conversations[conversationId]?.title ||
          conversationId,
        message,
        summary,
        saveState,
        openBraveTab,
      });
    }
  }
}

export async function completeInteractiveHtmlActions({
  client,
  options,
  browserState,
  browserRecord,
  conversationId,
  conversationTitle,
  message,
  summary,
  saveState,
  openBraveTab,
}) {
  if (message.artifacts.length === 0) {
    throw new Error(
      `Interactive HTML message ${message.id} has no downloadable HTML source.`,
    );
  }

  browserRecord.interactiveHtmlArtifacts ||= {};
  const artifactRecords =
    browserRecord.interactiveHtmlArtifacts[message.id] || {};
  for (const [index, artifact] of message.artifacts.entries()) {
    artifactRecords[artifact.key] ||= {
      sourceType: artifact.sourceType,
      ...(artifact.sandboxPath
        ? { sandboxPath: artifact.sandboxPath }
        : {}),
      localPath: interactiveHtmlLocalPath(
        options,
        conversationTitle,
        conversationId,
        message.id,
        artifact,
        index,
      ),
    };
  }
  browserRecord.interactiveHtmlArtifacts[message.id] = artifactRecords;
  browserState.conversations[conversationId] = browserRecord;
  await saveState(options.browserStatePath, browserState);

  const openedMessages = browserRecord.interactiveHtmlOpenedMessages || {};
  let conversationOpened = false;
  if (!openedMessages[message.id]) {
    await client.waitForBrowserOpen();
    openBraveTab(
      options,
      `https://chatgpt.com/c/${encodeURIComponent(conversationId)}`,
    );
    openedMessages[message.id] = new Date().toISOString();
    browserRecord.interactiveHtmlOpenedMessages = openedMessages;
    summary.interactiveHtmlTabsOpened += 1;
    conversationOpened = true;
    console.log(`Opened interactive HTML conversation: ${conversationTitle}`);
    await saveState(options.browserStatePath, browserState);
  }

  for (const artifact of message.artifacts) {
    const artifactRecord = artifactRecords[artifact.key];
    if (!artifactRecord.downloadedAt) {
      const download = await downloadInteractiveHtmlArtifact(
        client,
        conversationId,
        message.id,
        artifact,
      );
      await mkdir(path.dirname(artifactRecord.localPath), { recursive: true });
      await writeFile(artifactRecord.localPath, download.buffer, {
        mode: 0o600,
      });
      artifactRecord.downloadedAt = new Date().toISOString();
      artifactRecord.bytes = download.buffer.length;
      artifactRecord.contentType = download.contentType;
      summary.interactiveHtmlArtifactsDownloaded += 1;
      console.log(`Downloaded interactive HTML: ${artifactRecord.localPath}`);
      await saveState(options.browserStatePath, browserState);
    }
    if (!artifactRecord.openedAt) {
      openBraveTab(options, pathToFileURL(artifactRecord.localPath).href);
      artifactRecord.openedAt = new Date().toISOString();
      summary.interactiveHtmlArtifactTabsOpened += 1;
      console.log(`Opened local interactive HTML: ${artifactRecord.localPath}`);
      await saveState(options.browserStatePath, browserState);
    }
  }

  return { conversationOpened };
}

function interactiveHtmlArtifactsComplete(browserRecord, messageId) {
  const artifactRecords =
    browserRecord.interactiveHtmlArtifacts?.[messageId];
  return Boolean(
    artifactRecords &&
      Object.keys(artifactRecords).length > 0 &&
      Object.values(artifactRecords).every(
        (artifactRecord) =>
          artifactRecord.localPath &&
          artifactRecord.downloadedAt &&
          artifactRecord.openedAt,
      ),
  );
}

function interactiveHtmlMessageFromState(browserRecord, messageId) {
  const artifactRecords =
    browserRecord.interactiveHtmlArtifacts?.[messageId];
  if (!artifactRecords) return null;
  const artifacts = Object.entries(artifactRecords).map(
    ([artifactKey, artifactRecord]) => ({
      key: artifactKey,
      sourceType: artifactRecord.sourceType,
      sandboxPath: artifactRecord.sandboxPath,
      downloadedAt: artifactRecord.downloadedAt,
    }),
  );
  if (
    artifacts.length === 0 ||
    artifacts.some(
      (artifact) =>
        (artifact.sourceType === "sandbox" && !artifact.sandboxPath) ||
        (artifact.sourceType === "inline" && !artifact.downloadedAt) ||
        !["sandbox", "inline"].includes(artifact.sourceType),
    )
  ) {
    return null;
  }
  return { id: messageId, artifacts };
}

async function downloadInteractiveHtmlArtifact(
  client,
  conversationId,
  messageId,
  artifact,
) {
  if (artifact.sourceType === "inline") {
    return {
      buffer: Buffer.from(artifact.html, "utf8"),
      contentType: "text/html",
    };
  }

  const downloadInfo = await client.fetchInterpreterDownloadInfo(
    conversationId,
    messageId,
    artifact.sandboxPath,
  );
  if (downloadInfo.status !== "success" || !downloadInfo.download_url) {
    throw new Error(
      `ChatGPT could not download ${artifact.sandboxPath}: ${
        downloadInfo.error_code || downloadInfo.status || "unknown error"
      }`,
    );
  }
  const download = await client.fetchDownload(downloadInfo.download_url);
  const contentType = download.contentType.split(";")[0].trim().toLowerCase();
  if (contentType !== "text/html") {
    throw new Error(
      `ChatGPT returned ${download.contentType || "no content type"} for ${artifact.sandboxPath}.`,
    );
  }
  return download;
}

function interactiveHtmlArtifacts(content) {
  const artifactsByKey = new Map();
  visitStrings(content, (value) => {
    for (const match of value.matchAll(
      /\bsandbox:\/{1,2}[^\s)\]>"']+[.]html(?:[?#][^\s)\]>"']*)?/gi,
    )) {
      const sandboxPath = normalizedHtmlSandboxPath(match[0]);
      const key = `sandbox:${sandboxPath}`;
      artifactsByKey.set(key, {
        key,
        sourceType: "sandbox",
        sandboxPath,
      });
    }
  });

  if (artifactsByKey.size === 0 && contentDeclaresHtml(content)) {
    const html = firstInlineHtml(content);
    if (html) {
      artifactsByKey.set("inline:0", {
        key: "inline:0",
        sourceType: "inline",
        html,
      });
    }
  }
  return [...artifactsByKey.values()];
}

function normalizedHtmlSandboxPath(sandboxLink) {
  const encodedPath = sandboxLink
    .slice("sandbox:".length)
    .split(/[?#]/, 1)[0];
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(encodedPath);
  } catch {
    throw new Error(`Invalid sandbox HTML path: ${sandboxLink}`);
  }
  if (decodedPath.includes("\\") || decodedPath.includes("\0")) {
    throw new Error(`Unsafe sandbox HTML path: ${sandboxLink}`);
  }
  const normalizedPath = path.posix.normalize(decodedPath);
  if (
    !normalizedPath.startsWith("/mnt/data/") ||
    path.posix.extname(normalizedPath).toLowerCase() !== ".html"
  ) {
    throw new Error(`Unsafe sandbox HTML path: ${sandboxLink}`);
  }
  return normalizedPath;
}

function contentDeclaresHtml(value, fieldName = "") {
  if (typeof value === "string") {
    return (
      (fieldName === "content_type" || fieldName === "mime_type") &&
      value.toLowerCase() === "text/html"
    );
  }
  if (Array.isArray(value)) {
    return value.some((item) => contentDeclaresHtml(item));
  }
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(([key, item]) =>
    contentDeclaresHtml(item, key),
  );
}

function firstInlineHtml(content) {
  let html = null;
  visitStrings(content, (value) => {
    if (!html && /^\s*(?:<!doctype\s+html|<html\b)/i.test(value)) {
      html = value;
    }
  });
  return html;
}

function visitStrings(value, visitor) {
  if (typeof value === "string") {
    visitor(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) visitStrings(item, visitor);
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const item of Object.values(value)) visitStrings(item, visitor);
}

function interactiveHtmlLocalPath(
  options,
  conversationTitle,
  conversationId,
  messageId,
  artifact,
  artifactIndex,
) {
  const sourceName =
    artifact.sourceType === "sandbox"
      ? path.posix.basename(artifact.sandboxPath, ".html")
      : "interactive";
  const filename = [
    slugify(conversationTitle).slice(0, 50),
    shortId(conversationId),
    shortId(messageId),
    String(artifactIndex + 1),
    slugify(sourceName).slice(0, 50),
  ].join("--");
  return path.join(options.interactiveHtmlDirectory, `${filename}.html`);
}

function shortId(id) {
  return id.replace(/[^a-zA-Z0-9]/g, "").slice(0, 8) || "unknown";
}

function slugify(value) {
  const slug = String(value || "")
    .normalize("NFKD")
    .replace(/[^\x00-\x7F]/g, "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
  return slug || "untitled";
}

function timestampToMs(value) {
  if (typeof value === "number") {
    return value < 10_000_000_000 ? value * 1000 : value;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}
