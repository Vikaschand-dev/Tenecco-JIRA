"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  cloudId: null, siteUrl: null, projects: [], current: null,
  nextPageToken: null, lastJql: null, eventSource: null,
  board: null, dragging: null, boardTimer: null,
};
const BOARD_REFRESH_MS = 30000;

// ------------------------------------------------------------------ helpers

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), isError ? 7000 : 3000);
}

function errorText(data) {
  const d = data && data.detail;
  if (!d) return JSON.stringify(data);
  if (typeof d === "string") return d;
  if (d.errorMessages || d.errors) {
    return [...(d.errorMessages || []), ...Object.entries(d.errors || {}).map(([k, v]) => `${k}: ${v}`)].join("\n");
  }
  if (d.message) return d.message + (d.available ? `\nAvailable: ${d.available.join(", ")}` : "");
  return JSON.stringify(d);
}

async function api(method, path, body) {
  const url = new URL(path, location.origin);
  if (state.cloudId && path.startsWith("/api/")) url.searchParams.set("cloud_id", state.cloudId);
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && data && data.error === "not_connected") showWelcome();
    throw new Error(errorText(data) || res.statusText);
  }
  return data;
}

async function withBusy(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fillSelect(sel, items, { value = "id", label = "name", empty } = {}) {
  const prev = sel.value;
  sel.innerHTML = (empty !== undefined ? `<option value="">${esc(empty)}</option>` : "") +
    items.map((i) => `<option value="${esc(i[value])}">${esc(i[label])}</option>`).join("");
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

const splitLabels = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);

const store = {
  get(k, fallback) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("");
}
function avatarColor(name) {
  let h = 0;
  for (const c of name || "") h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 45% 45%)`;
}
function avatar(user) {
  if (!user) return `<span class="avatar" title="Unassigned">–</span>`;
  const name = esc(user.display_name);
  return `<span class="avatar" title="${name}" style="background:${avatarColor(user.display_name)}">${esc(initials(user.display_name))}</span>`;
}

// ------------------------------------------------------------------ connection

function showWelcome() {
  $("#app").hidden = true;
  $("#welcome").hidden = false;
  $("#connect-btn").hidden = false;
  $("#disconnect-btn").hidden = true;
  $("#site-select").hidden = true;
  $("#conn-status").textContent = "Not connected";
  $("#conn-status").className = "pill";
  clearInterval(state.boardTimer);
}

async function init() {
  const status = await fetch("/oauth/status", { credentials: "same-origin" }).then((r) => r.json());
  if (!status.connected || !status.sites.length) return showWelcome();

  $("#welcome").hidden = true;
  $("#app").hidden = false;
  $("#connect-btn").hidden = true;
  $("#disconnect-btn").hidden = false;
  $("#conn-status").textContent = "Connected";
  $("#conn-status").className = "pill ok";

  const siteSel = $("#site-select");
  fillSelect(siteSel, status.sites, { value: "cloud_id", label: "name" });
  siteSel.hidden = status.sites.length < 2;
  siteSel.onchange = () => selectSite(status.sites.find((s) => s.cloud_id === siteSel.value));
  await selectSite(status.sites[0]);
}

async function selectSite(site) {
  state.cloudId = site.cloud_id;
  state.siteUrl = site.url;
  closeDrawer();
  $("#hook-url").textContent = location.origin + "/webhooks/jira";

  // Load independently so one failing call doesn't blank the whole page.
  const projectsP = withBusy(null, async () => {
    state.projects = await api("GET", "/api/jira/projects");
    const items = state.projects.map((p) => ({ id: p.key, name: `${p.name} (${p.key})` }));
    fillSelect($("#b-project"), items);
    fillSelect($("#search-project"), items, { empty: "All projects" });
    fillSelect($("#c-project"), items);
    const saved = store.get("boardProject");
    if (saved && items.some((i) => i.id === saved)) $("#b-project").value = saved;
    if (items.length) await loadIssueTypes($("#c-project").value);
  });
  const prioritiesP = withBusy(null, async () => {
    const priorities = await api("GET", "/api/jira/priorities");
    fillSelect($("#c-priority"), priorities, { value: "name", empty: "Default" });
    fillSelect($("#d-priority"), priorities, { value: "name", empty: "—" });
  });

  await projectsP;
  if (state.projects.length) {
    loadBoard();
    withBusy(null, () => search());
  } else {
    $("#board").innerHTML = `<p class="muted">No projects visible to this account.</p>`;
  }
  await prioritiesP;
  connectStream();

  clearInterval(state.boardTimer);
  state.boardTimer = setInterval(() => {
    if (!document.hidden && !$("#tab-board").hidden && !state.dragging) loadBoard({ quiet: true });
  }, BOARD_REFRESH_MS);
}

$("#disconnect-btn").onclick = async () => {
  await fetch("/oauth/disconnect", { method: "POST", credentials: "same-origin" });
  if (state.eventSource) state.eventSource.close();
  showWelcome();
};

// ------------------------------------------------------------------ tabs

function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== "tab-" + name));
  if (name === "sync") loadSync();
  if (name === "board" && state.board) loadBoard({ quiet: true });
}
document.querySelectorAll(".tab").forEach((tab) => (tab.onclick = () => showTab(tab.dataset.tab)));

// ------------------------------------------------------------------ board

function columnOrderKey() { return `colOrder:${state.cloudId}:${$("#b-project").value}`; }

function orderedColumns() {
  const cols = state.board.columns;
  const saved = store.get(columnOrderKey(), []);
  return [...cols].sort((a, b) => {
    const ia = saved.indexOf(a.id), ib = saved.indexOf(b.id);
    if (ia === -1 && ib === -1) return cols.indexOf(a) - cols.indexOf(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

async function loadBoard({ quiet = false } = {}) {
  const project = $("#b-project").value;
  if (!project) return;
  store.set("boardProject", project);
  if (!quiet) $("#board").innerHTML = `<p class="muted">Loading board…</p>`;
  await withBusy(quiet ? null : $("#b-refresh"), async () => {
    const data = await api("GET", `/api/jira/projects/${encodeURIComponent(project)}/board`);
    if (project !== $("#b-project").value) return; // user switched project meanwhile
    state.board = data;
    const people = new Map();
    data.issues.forEach((i) => i.assignee && people.set(i.assignee.account_id, i.assignee.display_name));
    fillSelect(
      $("#b-assignee"),
      [{ id: "__none", name: "Unassigned" }, ...[...people].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name))],
      { empty: "All assignees" },
    );
    renderBoard();
    $("#b-updated").textContent = "Updated " + new Date().toLocaleTimeString();
  });
}

function visibleIssues() {
  const q = $("#b-search").value.trim().toLowerCase();
  const who = $("#b-assignee").value;
  const subtasks = $("#b-subtasks").checked;
  return state.board.issues.filter((i) =>
    (subtasks || !i.parent) &&
    (!who || (who === "__none" ? !i.assignee : i.assignee && i.assignee.account_id === who)) &&
    (!q || i.key.toLowerCase().includes(q) || (i.summary || "").toLowerCase().includes(q) ||
      (i.labels || []).some((l) => l.toLowerCase().includes(q))));
}

function cardHtml(i) {
  const prio = i.priority ? `<span class="prio prio-${esc(i.priority.toLowerCase())}" title="Priority">${esc(i.priority)}</span>` : "";
  return `
    <li class="card" draggable="true" tabindex="0" data-key="${esc(i.key)}">
      ${i.parent ? `<div class="parent">↳ ${esc(i.parent)}</div>` : ""}
      <p class="summary">${esc(i.summary)}</p>
      ${i.labels && i.labels.length ? `<div class="labels">${i.labels.map((l) => `<span class="label">${esc(l)}</span>`).join("")}</div>` : ""}
      <div class="foot">
        <span class="key">${esc(i.key)}</span>
        <span>${esc(i.issue_type || "")}</span>
        ${prio}
        ${avatar(i.assignee)}
      </div>
    </li>`;
}

function renderBoard() {
  const issues = visibleIssues();
  const cols = orderedColumns();
  $("#board").innerHTML = cols.map((c, idx) => {
    const items = issues.filter((i) => i.status === c.name);
    return `
      <div class="column cat-${esc(c.category)}" data-status="${esc(c.name)}" data-col="${esc(c.id)}">
        <div class="column-head">
          <span class="name">${esc(c.name)}</span>
          <span class="count">${items.length}</span>
          <span class="move">
            <button data-move="-1" ${idx === 0 ? "disabled" : ""} title="Move column left" aria-label="Move ${esc(c.name)} left">◀</button>
            <button data-move="1" ${idx === cols.length - 1 ? "disabled" : ""} title="Move column right" aria-label="Move ${esc(c.name)} right">▶</button>
          </span>
        </div>
        <ul class="cards">${items.map(cardHtml).join("") || `<li class="empty-col">No issues</li>`}</ul>
      </div>`;
  }).join("");
}

["#b-search", "#b-assignee", "#b-subtasks"].forEach((s) => $(s).addEventListener("input", () => state.board && renderBoard()));
$("#b-project").onchange = () => { state.board = null; loadBoard(); };
$("#b-refresh").onclick = () => loadBoard();
$("#b-create").onclick = async () => {
  $("#c-project").value = $("#b-project").value;
  await withBusy(null, () => loadIssueTypes($("#c-project").value));
  showTab("create");
  $("#c-summary").focus();
};

// Column reordering (saved per project in this browser)
$("#board").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-move]");
  if (btn) {
    const ids = orderedColumns().map((c) => c.id);
    const id = btn.closest(".column").dataset.col;
    const from = ids.indexOf(id), to = from + Number(btn.dataset.move);
    [ids[from], ids[to]] = [ids[to], ids[from]];
    store.set(columnOrderKey(), ids);
    renderBoard();
    return;
  }
  const card = e.target.closest(".card");
  if (card) openIssue(card.dataset.key);
});
$("#board").addEventListener("keydown", (e) => {
  const card = e.target.closest(".card");
  if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openIssue(card.dataset.key); }
});

// Drag and drop → status transition
$("#board").addEventListener("dragstart", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  state.dragging = card.dataset.key;
  card.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", card.dataset.key);
});
$("#board").addEventListener("dragend", (e) => {
  e.target.closest(".card")?.classList.remove("dragging");
  document.querySelectorAll(".column.drop-target").forEach((c) => c.classList.remove("drop-target"));
  state.dragging = null;
});
$("#board").addEventListener("dragover", (e) => {
  const col = e.target.closest(".column");
  if (!col || !state.dragging) return;
  e.preventDefault();
  document.querySelectorAll(".column.drop-target").forEach((c) => c !== col && c.classList.remove("drop-target"));
  col.classList.add("drop-target");
});
$("#board").addEventListener("drop", async (e) => {
  const col = e.target.closest(".column");
  const key = state.dragging;
  if (!col || !key) return;
  e.preventDefault();
  col.classList.remove("drop-target");
  const issue = state.board.issues.find((i) => i.key === key);
  const target = col.dataset.status;
  if (!issue || issue.status === target) return;

  const previous = issue.status;
  issue.status = target; // optimistic move
  renderBoard();
  const cardEl = document.querySelector(`.card[data-key="${CSS.escape(key)}"]`);
  cardEl?.classList.add("saving");
  try {
    const updated = await api("POST", `/api/jira/issues/${encodeURIComponent(key)}/status`, { status: target });
    Object.assign(issue, updated);
    toast(`${key} → ${updated.status}`);
  } catch (err) {
    issue.status = previous;
    toast(`Couldn't move ${key} to ${target}.\n${err.message}`, true);
  }
  renderBoard();
});

function patchBoardIssue(updated) {
  if (!state.board) return;
  const idx = state.board.issues.findIndex((i) => i.key === updated.key);
  if (idx >= 0) { state.board.issues[idx] = { ...state.board.issues[idx], ...updated }; renderBoard(); }
}

// ------------------------------------------------------------------ issues list

function buildJql() {
  const project = $("#search-project").value;
  let jql = $("#search-jql").value.trim();
  if (project) {
    const order = jql.match(/\sORDER BY .*/i);
    const where = order ? jql.slice(0, order.index).trim() : jql;
    jql = `project = "${project}"` + (where ? ` AND (${where})` : "") + (order ? order[0] : "");
  }
  if (!jql) jql = "updated >= -30d";
  if (!/ORDER BY/i.test(jql)) jql += " ORDER BY updated DESC";
  return jql;
}

async function search(append = false) {
  const jql = append ? state.lastJql : buildJql();
  state.lastJql = jql;
  const data = await api("POST", "/api/jira/issues/search", {
    jql, max_results: 25, next_page_token: append ? state.nextPageToken : null,
  });
  state.nextPageToken = data.next_page_token;
  $("#load-more").hidden = data.is_last || !data.next_page_token;
  const html = data.issues.map((i) => `
    <li data-key="${esc(i.key)}">
      <span class="key">${esc(i.key)}</span>
      <span class="sum" title="${esc(i.summary)}">${esc(i.summary)}</span>
      <span class="pill">${esc(i.status)}</span>
    </li>`).join("");
  const list = $("#issue-list");
  list.innerHTML = append ? list.innerHTML + html : html || `<li class="muted">No issues found</li>`;
}

$("#search-form").onsubmit = (e) => { e.preventDefault(); withBusy(e.submitter, () => search()); };
$("#load-more").onclick = (e) => withBusy(e.target, () => search(true));
$("#issue-list").onclick = (e) => {
  const li = e.target.closest("li[data-key]");
  if (li) openIssue(li.dataset.key);
};

// ------------------------------------------------------------------ detail drawer

function openDrawer() { $("#detail").hidden = false; $("#drawer-backdrop").hidden = false; }
function closeDrawer() { $("#detail").hidden = true; $("#drawer-backdrop").hidden = true; state.current = null; }
$("#d-close").onclick = closeDrawer;
$("#drawer-backdrop").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#detail").hidden) closeDrawer(); });

async function openIssue(key) {
  document.querySelectorAll("#issue-list li").forEach((li) => li.classList.toggle("active", li.dataset.key === key));
  await withBusy(null, async () => {
    const [issue, transitions, comments] = await Promise.all([
      api("GET", `/api/jira/issues/${key}`),
      api("GET", `/api/jira/issues/${key}/transitions`),
      api("GET", `/api/jira/issues/${key}/comments`),
    ]);
    state.current = issue;
    renderIssue(issue);
    fillSelect($("#d-transition"), transitions.map((t) => ({ id: t.id, name: `${t.name} → ${t.to_status}` })));
    renderComments(comments);
    $("#d-user").innerHTML = "";
    $("#d-user-q").value = "";
    openDrawer();
  });
}

function renderIssue(i) {
  $("#d-key").textContent = i.key;
  $("#d-key").href = `${state.siteUrl}/browse/${i.key}`;
  $("#d-parent").textContent = i.parent ? `↳ ${i.parent}` : "";
  $("#d-status").textContent = i.status;
  $("#d-type").textContent = i.issue_type;
  $("#d-summary").value = i.summary || "";
  $("#d-description").value = i.description || "";
  $("#d-priority").value = i.priority || "";
  $("#d-labels").value = (i.labels || []).join(", ");
  $("#d-assignee").textContent = i.assignee ? `— ${i.assignee.display_name}` : "— Unassigned";
  const li = document.querySelector(`#issue-list li[data-key="${CSS.escape(i.key)}"]`);
  if (li) {
    li.querySelector(".sum").textContent = i.summary;
    li.querySelector(".pill").textContent = i.status;
  }
  patchBoardIssue(i);
}

function renderComments(comments) {
  $("#comments").innerHTML = comments.map((c) => `
    <li>
      <div class="meta">${esc(c.author?.display_name)} · ${new Date(c.created).toLocaleString()}</div>
      <div class="body">${esc(c.body)}</div>
    </li>`).join("") || `<li class="muted">No comments</li>`;
}

$("#edit-form").onsubmit = (e) => {
  e.preventDefault();
  const key = state.current.key;
  withBusy(e.submitter, async () => {
    const body = {
      summary: $("#d-summary").value,
      description: $("#d-description").value,
      labels: splitLabels($("#d-labels").value),
    };
    if ($("#d-priority").value) body.priority = $("#d-priority").value;
    renderIssue(await api("PATCH", `/api/jira/issues/${key}`, body));
    toast(`${key} updated`);
  });
};

$("#d-transition-btn").onclick = (e) => {
  const key = state.current.key;
  withBusy(e.target, async () => {
    const issue = await api("POST", `/api/jira/issues/${key}/status`, { transition_id: $("#d-transition").value });
    toast(`${key} → ${issue.status}`);
    await openIssue(key); // available transitions depend on the new status
  });
};

let userSearchTimer;
$("#d-user-q").oninput = () => {
  clearTimeout(userSearchTimer);
  userSearchTimer = setTimeout(() => withBusy(null, async () => {
    const users = await api("GET", `/api/jira/users/assignable?issue_key=${encodeURIComponent(state.current.key)}&query=${encodeURIComponent($("#d-user-q").value)}`);
    fillSelect($("#d-user"), users, { value: "account_id", label: "display_name", empty: "Unassigned" });
  }), 300);
};

$("#d-assign-btn").onclick = (e) => {
  const key = state.current.key;
  withBusy(e.target, async () => {
    const issue = await api("PUT", `/api/jira/issues/${key}/assignee`, { account_id: $("#d-user").value || null });
    renderIssue(issue);
    toast(`${key} assigned to ${issue.assignee ? issue.assignee.display_name : "nobody"}`);
  });
};

$("#d-delete").onclick = (e) => {
  const key = state.current.key;
  if (!confirm(`Delete ${key}? This cannot be undone.`)) return;
  withBusy(e.target, async () => {
    await api("DELETE", `/api/jira/issues/${key}`);
    closeDrawer();
    document.querySelector(`#issue-list li[data-key="${CSS.escape(key)}"]`)?.remove();
    if (state.board) { state.board.issues = state.board.issues.filter((i) => i.key !== key); renderBoard(); }
    toast(`${key} deleted`);
  });
};

$("#comment-form").onsubmit = (e) => {
  e.preventDefault();
  const key = state.current.key;
  withBusy(e.submitter, async () => {
    await api("POST", `/api/jira/issues/${key}/comments`, { body: $("#comment-body").value });
    $("#comment-body").value = "";
    renderComments(await api("GET", `/api/jira/issues/${key}/comments`));
  });
};

// ------------------------------------------------------------------ create

async function loadIssueTypes(projectKey) {
  const types = await api("GET", `/api/jira/projects/${encodeURIComponent(projectKey)}/issue-types`);
  fillSelect($("#c-type"), types.filter((t) => !t.subtask));
}
$("#c-project").onchange = () => withBusy(null, () => loadIssueTypes($("#c-project").value));

$("#create-form").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () => {
    const body = {
      project_key: $("#c-project").value,
      issue_type: $("#c-type").value,
      summary: $("#c-summary").value,
      description: $("#c-description").value || null,
      priority: $("#c-priority").value || null,
      labels: splitLabels($("#c-labels").value),
    };
    const issue = await api("POST", "/api/jira/issues", body);
    toast(`Created ${issue.key}`);
    e.target.reset();
    await loadIssueTypes($("#c-project").value);
    if (body.project_key === $("#b-project").value) {
      showTab("board");
      await loadBoard({ quiet: true });
    } else {
      showTab("issues");
      await search();
    }
    await openIssue(issue.key);
  });
};

// ------------------------------------------------------------------ webhook sync

async function loadSync() {
  await withBusy(null, async () => {
    const [hooks, synced, events] = await Promise.all([
      api("GET", "/api/jira/webhooks"),
      api("GET", "/api/jira/synced-issues"),
      api("GET", "/api/jira/events?limit=30"),
    ]);
    $("#hooks").innerHTML = hooks.map((h) => `
      <li>
        <span><code>${esc(h.jql_filter)}</code><br><span class="muted">${esc(h.events.join(", "))}${h.expires_at ? " · expires " + new Date(h.expires_at).toLocaleDateString() : ""}</span></span>
        <button class="btn danger small" data-hook="${esc(h.id)}">Remove</button>
      </li>`).join("") || `<li class="muted">No subscriptions yet</li>`;
    $("#synced").innerHTML = synced.map((s) => `
      <tr><td>${esc(s.key)}</td><td>${s.deleted ? "<s>" + esc(s.summary) + "</s>" : esc(s.summary)}</td><td>${esc(s.status)}</td><td>${esc(s.assignee || "")}</td></tr>`).join("")
      || `<tr><td colspan="4" class="muted">Nothing synced yet</td></tr>`;
    if (!$("#events").children.length) {
      $("#events").innerHTML = events.map((ev) => eventItem({ event: ev.event, issue_key: ev.issue_key }, ev.received_at)).join("");
    }
  });
}

$("#hook-form").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () => {
    const res = await api("POST", "/api/jira/webhooks", { jql_filter: $("#hook-jql").value });
    toast(res.errors.length ? `Partially created: ${JSON.stringify(res.errors)}` : "Subscribed");
    $("#hook-jql").value = "";
    await loadSync();
  });
};

$("#hooks").onclick = (e) => {
  const id = e.target.dataset.hook;
  if (!id) return;
  withBusy(e.target, async () => { await api("DELETE", `/api/jira/webhooks/${id}`); await loadSync(); });
};

$("#hook-refresh").onclick = (e) => withBusy(e.target, async () => {
  const r = await api("POST", "/api/jira/webhooks/refresh");
  toast(`Refreshed ${r.refreshed.length} webhook(s)`);
  await loadSync();
});

function eventItem(ev, when = new Date()) {
  const changes = (ev.changes || []).map((c) => `${c.field}: ${c.from ?? "∅"} → ${c.to ?? "∅"}`).join("; ");
  const extra = ev.comment ? `💬 ${ev.comment.author}: ${ev.comment.body}` : changes;
  return `<li>
    <div><strong>${esc(ev.issue_key || "")}</strong> ${esc(ev.event)}${ev.actor ? " by " + esc(ev.actor) : ""}</div>
    ${extra ? `<div>${esc(extra)}</div>` : ""}
    <div class="meta">${new Date(when).toLocaleString()}</div>
  </li>`;
}

function connectStream() {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource(`/api/jira/events/stream?cloud_id=${encodeURIComponent(state.cloudId)}`);
  state.eventSource = es;
  es.onopen = () => $("#live-dot").classList.add("live");
  es.onerror = () => $("#live-dot").classList.remove("live");
  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    $("#events").insertAdjacentHTML("afterbegin", eventItem(ev));
    // Keep the open issue fresh when it changes in Jira.
    // (Skipped while the user is mid-edit so their unsaved changes aren't overwritten.)
    const editing = document.activeElement && document.activeElement.closest("#edit-form");
    if (state.current && ev.issue_key === state.current.key && ev.event !== "jira:issue_deleted" && !editing) {
      openIssue(ev.issue_key);
    }
    if (!$("#tab-board").hidden && !state.dragging) loadBoard({ quiet: true });
    if (!$("#tab-sync").hidden) loadSync();
  };
}

init().catch((e) => toast(e.message, true));
