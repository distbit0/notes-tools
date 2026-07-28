const CONVERSATIONS_PAGE_SIZE = 100;
const PROJECT_CONVERSATIONS_PAGE_SIZE = 20;

export async function listAllActiveConversations(client, maxConversations = null) {
  const options = {
    cutoffMs: 0,
    maxConversations,
  };
  const projects = await fetchProjects(client);
  const { candidates } = await collectConversationCandidates(
    client,
    options,
    projects,
  );
  return candidates;
}

export async function collectConversationCandidates(
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

export async function fetchProjects(client) {
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
    if (page.oldestUpdateTimeMs < minimumUpdateTimeMs) break;
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

function hasReachedCandidateLimit(options, candidatesById) {
  return (
    options.maxConversations !== null &&
    candidatesById.size >= options.maxConversations
  );
}

function normalizeConversationListItem(item, project) {
  const id = item.id || item.conversation_id;
  if (!id) throw new Error("Conversation list item missing id");
  return {
    id,
    title: item.title || "Untitled",
    createTimeMs: timestampToMs(item.create_time || item.created_at),
    updateTimeMs: timestampToMs(item.update_time || item.updated_at),
    project,
  };
}

export function timestampToMs(value) {
  if (typeof value === "number") {
    return value < 10_000_000_000 ? value * 1000 : value;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}
