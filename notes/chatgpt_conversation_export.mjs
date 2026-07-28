import { existsSync } from "node:fs";
import {
  appendFile,
  mkdir,
  readFile,
  readdir,
  writeFile,
} from "node:fs/promises";
import path from "node:path";

import { UserFacingError } from "./chatgpt_backend_client.mjs";
import { timestampToMs } from "./chatgpt_conversation_listing.mjs";

const MARKDOWN_FORMAT_VERSION = 3;
const PERMANENT_ATTACHMENT_STATUSES = new Set([
  "downloaded",
  "file_not_found",
  "access_denied",
]);

export async function exportConversation(
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

export function messageIdsToPersist(conversation, previousRecord) {
  const visibleIds = visibleMessageIds(conversation).filter(
    (messageId) => conversation.mapping?.[messageId]?.message,
  );
  if (!previousRecord?.id) return visibleIds;

  if (!Array.isArray(previousRecord.seenMessageIds)) return [];

  const seenMessageIds = new Set(previousRecord.seenMessageIds);
  return visibleIds.filter((messageId) => !seenMessageIds.has(messageId));
}

export function extractFileReferences(conversation, messageIds = null) {
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

export function conversationToMarkdown(conversation, candidate, fileMap, messageIds = null) {
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
