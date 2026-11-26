"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const fragment = new URLSearchParams(location.hash.slice(1));
  let token =
    fragment.get("token") || sessionStorage.getItem("agent-bus-monitor-token");
  if (fragment.has("token")) {
    sessionStorage.setItem("agent-bus-monitor-token", token);
    history.replaceState(null, "", location.pathname + location.search);
  }
  // The page agent-bus monitor --web opens carries a single-use ticket, not the token,
  // so the token is in no opened URL. start() trades the ticket for it.
  const ticket = fragment.get("ticket");
  if (ticket)
    history.replaceState(null, "", location.pathname + location.search);
  const state = {
    cursor: null,
    events: [],
    frame: null,
    paused: false,
    debug: false,
    selected: null,
    busy: false,
    timer: null,
    failures: 0,
    fetchMs: null,
  };
  const terminal = new Set([
    "completed",
    "failed",
    "abandoned",
    "cancelled",
    "uncertain",
  ]);
  const pad = (value) => String(value).padStart(2, "0");
  // A moment in this browser's time zone: the time alone on the viewer's current
  // calendar day, the date and the time on any other day.
  const time = (seconds) => {
    const at = new Date(seconds * 1000);
    const now = new Date();
    const clock = `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
    const day = (date) =>
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    return day(at) === day(now) ? clock : `${day(at)} ${clock}`;
  };
  const zone = () => {
    const minutes = -new Date().getTimezoneOffset();
    const sign = minutes < 0 ? "-" : "+";
    return `UTC${sign}${pad(Math.floor(Math.abs(minutes) / 60))}:${pad(Math.abs(minutes) % 60)}`;
  };
  const text = (id, value) => {
    $(id).textContent = value;
  };
  const count = (value) => Number(value || 0).toLocaleString();
  const short = (value) => (value ? value.slice(0, 8) : "—");
  function notice(message) {
    $("notice").hidden = !message;
    text("notice", message || "");
  }
  function connection(label, kind = "") {
    $("connection").className = "connection " + kind;
    $("connection").querySelector("span").textContent = label;
  }
  function updateDebug() {
    $("debug-pane").hidden = !state.debug;
    $("debug-toggle").setAttribute("aria-pressed", String(state.debug));
    const frame = state.frame;
    text(
      "diagnostics",
      JSON.stringify(
        {
          version: frame?.version,
          namespace: frame?.namespace,
          ledger_available: frame?.available,
          bus_configured: frame?.configured,
          connection: frame?.connection,
          cursor: state.cursor,
          latest_cursor: frame?.latest_cursor,
          snapshot_ms: frame?.read_ms,
          browser_fetch_ms: state.fetchMs,
          consecutive_fetch_failures: state.failures,
          web: frame?.web,
          ledger_error: frame?.error,
        },
        null,
        2,
      ),
    );
    const event = state.selected || state.events.at(-1);
    text(
      "selected-title",
      state.selected ? `Event #${state.selected.seq}` : "Latest event",
    );
    text(
      "event-detail",
      event ? JSON.stringify(event, null, 2) : "No event selected",
    );
  }
  function renderEvents() {
    const search = $("search").value.toLowerCase().trim();
    const category = $("filter").value;
    const filtered = state.events.filter(
      (event) =>
        (category === "all" || event.type.startsWith(category + ".")) &&
        [
          event.type,
          event.peer_id,
          event.command_id,
          event.project,
          event.payload.task_id || "",
        ].some((value) => value.toLowerCase().includes(search)),
    );
    const fragment = document.createDocumentFragment();
    for (const event of [...filtered].reverse()) {
      const row = document.createElement("tr");
      row.dataset.testid = "event-row";
      row.dataset.commandId = event.command_id;
      const eventCell = document.createElement("td");
      const button = document.createElement("button");
      const tone = /failed|rejected/.test(event.type)
        ? "error"
        : /uncertain|expired/.test(event.type)
          ? "warning"
          : event.type.endsWith("completed")
            ? "success"
            : "";
      button.className = "event-button " + tone;
      button.setAttribute("aria-label", `Inspect event ${event.seq}`);
      const dot = document.createElement("i");
      const label = document.createElement("span");
      label.textContent = event.type;
      button.append(dot, label);
      button.addEventListener("click", () => {
        state.selected = event;
        state.debug = true;
        updateDebug();
      });
      eventCell.append(button);
      const peerCell = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "peer-badge";
      badge.textContent = event.peer_id || "system";
      badge.title = event.peer_id;
      peerCell.append(badge);
      const commandCell = document.createElement("td");
      commandCell.className = "code";
      commandCell.textContent = short(event.command_id);
      commandCell.title = event.command_id;
      const timeCell = document.createElement("td");
      timeCell.className = "time-cell";
      timeCell.textContent = time(event.created_at);
      timeCell.title = new Date(event.created_at * 1000).toISOString();
      row.append(eventCell, peerCell, commandCell, timeCell);
      fragment.append(row);
    }
    $("events").replaceChildren(fragment);
    $("empty").hidden = filtered.length > 0;
    $("empty").querySelector("h3").textContent = state.events.length
      ? "No events match this filter."
      : "No events yet";
    $("empty").querySelector("p").textContent = state.events.length
      ? "Try another event type, peer or command ID."
      : "Your next authorised command will appear here.";
    text(
      "event-summary",
      `${count(filtered.length)} visible / ${count(state.frame?.stats?.events)} total`,
    );
    text(
      "cursor-label",
      `Cursor ${state.cursor === null ? "—" : "#" + state.cursor}`,
    );
  }
  function chart(activity = [], now = Date.now() / 1000) {
    const canvas = $("activity-chart");
    const width = canvas.clientWidth,
      height = 80,
      ratio = devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const bins = Array(30).fill(0);
    for (const stamp of activity) {
      const bin = Math.floor((stamp - (now - 60)) / 2);
      if (bin >= 0 && bin < bins.length) bins[bin] += 1;
    }
    const peak = Math.max(1, ...bins);
    ctx.strokeStyle = "#2a323d";
    ctx.lineWidth = 1;
    for (const y of [12, 40, 68]) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }
    const points = bins.map((value, index) => [
      3 + (index * (width - 6)) / 29,
      68 - (value / peak) * 51,
    ]);
    ctx.beginPath();
    ctx.moveTo(points[0][0], 78);
    for (const [x, y] of points) ctx.lineTo(x, y);
    ctx.lineTo(points.at(-1)[0], 78);
    ctx.closePath();
    ctx.fillStyle = "rgba(154,213,172,.07)";
    ctx.fill();
    ctx.beginPath();
    points.forEach(([x, y], index) =>
      index ? ctx.lineTo(x, y) : ctx.moveTo(x, y),
    );
    ctx.strokeStyle = "#9ad5ac";
    ctx.lineWidth = 1.8;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(...points.at(-1), 2.5, 0, Math.PI * 2);
    ctx.fillStyle = "#9ad5ac";
    ctx.fill();
  }
  function render(frame) {
    state.frame = frame;
    if (frame.reset_required) {
      notice(
        "The event history changed. Reconnecting from the latest available events.",
      );
      state.cursor = 0;
      state.events = [];
      state.selected = null;
      return;
    }
    if (!frame.available) {
      // Only an explicit false is the opt-in state; a frame without the key is a failure.
      if (frame.configured === false) {
        connection("Bus not configured", "waiting");
        notice(
          "The agent bus is not configured, so there are no lifecycle events to show. Run agent-bus configure to enable it.",
        );
      } else {
        connection("Ledger unavailable", "error");
        notice(
          "The local event ledger is unavailable. Previously observed events remain visible.",
        );
      }
      for (const id of [
        "commands-count",
        "active-count",
        "completed-count",
        "attention-count",
      ])
        text(id, "—");
      updateDebug();
      return;
    }
    notice("");
    state.cursor = frame.cursor;
    const retained = new Map(state.events.map((event) => [event.seq, event]));
    for (const event of frame.events) retained.set(event.seq, event);
    state.events = [...retained.values()]
      .sort((a, b) => a.seq - b.seq)
      .slice(-200);
    const stats = frame.stats,
      delivery = stats.delivery,
      tasks = stats.tasks;
    const active =
      (delivery.queued || 0) +
      (delivery.dispatching || 0) +
      Object.entries(tasks).reduce(
        (sum, [name, value]) => sum + (terminal.has(name) ? 0 : value),
        0,
      );
    const attention =
      (delivery.uncertain || 0) +
      (delivery.rejected || 0) +
      (delivery.expired || 0) +
      (tasks.failed || 0) +
      (tasks.uncertain || 0) +
      (tasks.blocked || 0);
    text("commands-count", count(stats.commands));
    text("active-count", count(active));
    text("completed-count", count(tasks.completed));
    text("attention-count", count(attention));
    text(
      "activity-count",
      count(stats.activity.length) + (stats.activity_limited ? "+" : ""),
    );
    text("pending-badge", `${count(stats.outbox_pending)} to publish`);
    const rows = document.createDocumentFragment();
    for (const [key, label] of [
      ["queued", "Queued"],
      ["dispatching", "Dispatching"],
      ["bound", "Bound"],
      ["uncertain", "Uncertain"],
    ]) {
      const row = document.createElement("div");
      row.className = "delivery-row";
      const name = document.createElement("span");
      name.textContent = label;
      const bar = document.createElement("progress");
      bar.max = Math.max(1, stats.commands);
      bar.value = delivery[key] || 0;
      bar.setAttribute("aria-label", label);
      const value = document.createElement("strong");
      value.textContent = count(delivery[key]);
      row.append(name, bar, value);
      rows.append(row);
    }
    $("delivery-rows").replaceChildren(rows);
    const status = frame.connection;
    connection(
      status.broker_connected
        ? "Bus connected"
        : status.service_running === true
          ? "Broker offline"
          : "Status unavailable",
      status.broker_connected ? "" : "waiting",
    );
    text("last-updated", `Updated ${time(frame.generated_at)} · 1s refresh`);
    text("version-label", `Answerback ${frame.version}`);
    chart(stats.activity, frame.generated_at);
    renderEvents();
    updateDebug();
  }
  async function poll() {
    if (state.paused || state.busy) return;
    state.busy = true;
    const started = performance.now();
    try {
      const query = state.cursor === null ? "" : `?after=${state.cursor}`;
      const response = await fetch("/api/frame" + query, {
        headers: { Authorization: "Bearer " + token },
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      });
      if (!response.ok)
        throw new Error(response.status === 401 ? "access" : "network");
      const frame = await response.json();
      if (!state.paused) {
        state.failures = 0;
        state.fetchMs = Math.round(performance.now() - started);
        render(frame);
      }
    } catch (error) {
      if (!state.paused) {
        state.failures += 1;
        connection(
          error.message === "access" ? "Access needed" : "Reconnecting",
          "error",
        );
        notice(
          error.message === "access"
            ? "Open the private URL printed by agent-bus monitor --web to access this session."
            : "The monitor connection was interrupted. Retrying with the last event cursor.",
        );
        updateDebug();
      }
    } finally {
      state.busy = false;
      clearTimeout(state.timer);
      if (!state.paused)
        state.timer = setTimeout(poll, state.frame?.has_more ? 50 : 1000);
    }
  }
  // The board: a fold over the bridge's AG-UI 1.0 event stream (/api/events, SSE).
  // Stream content is untrusted: it is only ever written through textContent.
  const board = {
    state: null,
    activities: {},
    recent: [],
    last: null,
    history: false,
    page: 0,
    pageData: null,
    pageRequest: null,
    pageError: null,
  };
  const WINDOW_MS = 24 * 60 * 60 * 1000;
  const RUN_PAGE = 100;
  const done = new Set(["completed", "failed", "abandoned", "cancelled"]);
  // The default view: the last 24 hours, plus anything unfinished or needing attention
  // whatever its age. "All" shows every retained run. board.py applies the same rule.
  const inWindow = (run, now) =>
    !done.has(run.status) || run.attention === true || run.updatedMs >= now - WINDOW_MS;
  const LIFECYCLE = /^(RUN|STEP|SUBAGENT)_|^CUSTOM$/;
  const tokens = (path) =>
    path
      .split("/")
      .slice(1)
      .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
  function applyPatch(document_, operations) {
    let result = structuredClone(document_);
    for (const operation of operations) {
      const path = tokens(operation.path);
      if (!path.length) {
        result = structuredClone(operation.value);
        continue;
      }
      let parent = result;
      for (const key of path.slice(0, -1)) {
        if (parent === null || typeof parent !== "object" || !(key in parent))
          throw new Error("patch");
        parent = parent[key];
      }
      const key = path.at(-1);
      if (parent === null || typeof parent !== "object")
        throw new Error("patch");
      if (operation.op === "add") {
        if (Array.isArray(parent))
          parent.splice(key === "-" ? parent.length : Number(key), 0, operation.value);
        else parent[key] = operation.value;
      } else if (!(key in parent)) throw new Error("patch");
      else if (operation.op === "remove") {
        if (Array.isArray(parent)) parent.splice(Number(key), 1);
        else delete parent[key];
      } else if (operation.op === "replace") parent[key] = operation.value;
      else throw new Error("patch");
    }
    return result;
  }
  // An activity the state does not list has ended. Dropping it bounds what this page holds.
  const MAX_ACTIVITIES = 64;
  const listed = () => board.state?.activities || {};
  function fold(id, event) {
    if (event.type === "STATE_SNAPSHOT") {
      // A snapshot is a complete restart: the live activities follow it again.
      board.state = event.snapshot;
      board.activities = {};
      board.recent = []; // The connect page restates recent events after it.
    } else if (event.type === "STATE_DELTA") {
      if (board.state === null) throw new Error("patch");
      board.state = applyPatch(board.state, event.delta);
      for (const key of Object.keys(board.activities))
        if (!(key in listed())) delete board.activities[key];
    } else if (event.type === "ACTIVITY_SNAPSHOT") {
      if (
        event.messageId in listed() &&
        (event.messageId in board.activities ||
          Object.keys(board.activities).length < MAX_ACTIVITIES)
      )
        board.activities[event.messageId] = event.content;
    } else if (event.type === "ACTIVITY_DELTA") {
      if (event.messageId in board.activities)
        board.activities[event.messageId] = applyPatch(
          board.activities[event.messageId],
          event.patch,
        );
    } else if (LIFECYCLE.test(event.type)) {
      const tags = event.metadata?.answerback || {};
      // The timestamp is formatted when the board is drawn, so "today" is that day.
      board.recent.push([
        event.timestamp || null,
        event.type,
        tags.threadId || event.name || "—",
        short(tags.runId),
        event.stepName ||
          event.message ||
          event.value?.message ||
          event.outcome?.type ||
          tags.peerId ||
          "—",
      ]);
      board.recent = board.recent.slice(-40);
    }
    board.last = id;
  }
  const shown = (value) =>
    value === null || value === undefined || value === ""
      ? "—"
      : value === true
        ? "yes"
        : value === false
          ? "no"
          : String(value);
  const tone = (value) =>
    /^(completed|idle|RUN_FINISHED|SUBAGENT_FINISHED)$/.test(value)
      ? "tone-success"
      : /^(failed|RUN_ERROR|SUBAGENT_ERROR)$/.test(value)
        ? "tone-error"
        : /^(blocked|uncertain|abandoned|cancelled|overdue)$/.test(value)
          ? "tone-warning"
          : "";
  function rows(id, items, columns, toned = -1, debugging = {}) {
    const body = $(id);
    const fragment = document.createDocumentFragment();
    for (const cells of items) {
      const row = document.createElement("tr");
      row.dataset.testid = id + "-row";
      cells.forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = shown(value);
        cell.title = cell.textContent;
        if ((id === "board-peers" && index === 0) || (id === "board-runs" && index === 2)) {
          const until = debugging[value];
          if (Number.isFinite(until) && until > Date.now()) {
            const badge = document.createElement("span");
            badge.className = "debug-badge";
            badge.textContent = " DEBUG";
            badge.title = `Debug until ${new Date(until).toLocaleString()}`;
            cell.append(badge);
          }
        }
        if (index === toned) cell.className = tone(cell.textContent);
        row.append(cell);
      });
      fragment.append(row);
    }
    if (!items.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = columns;
      cell.className = "empty-row";
      cell.textContent = "Nothing to show";
      row.append(cell);
      fragment.append(row);
    }
    body.replaceChildren(fragment);
    text(id + "-count", count(items.length));
  }
  async function loadRunPage() {
    if (board.pageRequest) return;
    const request = new AbortController();
    board.pageRequest = request;
    const offset = board.page * RUN_PAGE;
    try {
      const response = await fetch(`/api/runs?offset=${offset}&limit=${RUN_PAGE}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: request.signal,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("history");
      const page = await response.json();
      if (board.pageRequest !== request) return;
      board.pageData = page; // Replace the previous page; never accumulate history.
      board.pageError = null;
    } catch (error) {
      if (board.pageRequest !== request) return;
      board.pageError = "Receipt history unavailable. Select All to retry.";
    } finally {
      if (board.pageRequest === request) {
        board.pageRequest = null;
        renderBoard();
      }
    }
  }
  function renderBoard() {
    const current = board.state;
    if (!current) return;
    const stored = board.history && current.bridge?.runsPageable;
    if (stored && !board.pageData) {
      rows("board-runs", [], 6);
      text("board-runs-note", board.pageError || "Loading retained receipt history…");
      if (!board.pageError) loadRunPage();
      return;
    }
    const tails = {};
    // By run, not by peer: a worker's earlier run says nothing about its next one.
    for (const content of Object.values(board.activities))
      tails[content.runId] = content;
    const retained = Object.entries(stored ? board.pageData.runs : current.runs || {});
    const now = Date.now();
    const runs = retained
      .filter(([, run]) => board.history || inWindow(run, now))
      .sort(
        ([, a], [, b]) =>
          done.has(a.status) - done.has(b.status) || b.updatedMs - a.updatedMs,
      );
    const total = stored ? board.pageData.total : retained.length;
    const selected = stored ? total : runs.length;
    const pages = Math.max(1, Math.ceil(selected / RUN_PAGE));
    if (!stored) board.page = Math.min(board.page, pages - 1);
    rows(
      "board-runs",
      (stored ? runs : runs.slice(board.page * RUN_PAGE, (board.page + 1) * RUN_PAGE)).map(([id, run]) => [
        run.threadId,
        short(id),
        run.peerId,
        run.status,
        time(run.updatedMs / 1000),
        tails[id]?.changedMs ? time(tails[id].changedMs / 1000) : null,
      ]),
      6,
      3,
      current.bridge?.debug?.peers || {},
    );
    text("board-runs-count", `${count(selected)} of ${count(total)}`);
    const omitted = current.bridge?.runsOmitted || 0;
    text(
      "board-runs-note",
      (board.history
        ? "Every retained run." + (stored ? " Read from the receipt store; select All to refresh." : "")
        : "Last 24 hours, plus unfinished and failed runs of any age." +
          (retained.length > runs.length
            ? ` ${count(retained.length - runs.length)} older in All.`
            : "")) + (omitted && !stored ? ` truncated, ${count(omitted)} more; All reads retained history.` : ""),
    );
    $("board-window").setAttribute("aria-pressed", String(!board.history));
    $("board-all").setAttribute("aria-pressed", String(board.history));
    $("board-pager").hidden = pages < 2;
    text("board-page", `Page ${board.page + 1} of ${pages}`);
    $("board-newer").disabled = board.page === 0;
    $("board-older").disabled = board.page >= pages - 1;
    rows(
      "board-peers",
      Object.entries(current.peers || {})
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([id, peer]) => [
          id,
          peer.name,
          peer.agentKind,
          peer.state,
          peer.sendReady,
          peer.runId ? short(peer.runId) : null,
          peer.metrics?.tokens,
          peer.metrics?.cpuPercent,
          peer.metrics?.rssBytes == null ? null : (peer.metrics.rssBytes / 1048576).toFixed(1),
          peer.metrics?.children,
          peer.trace?.sinceMs ? time(peer.trace.sinceMs / 1000) : null,
          peer.trace?.fresh === false ? "unknown" : peer.trace?.anomalies?.join(", "),
        ]),
      12,
      3,
      current.bridge?.debug?.peers || {},
    );
    rows(
      "board-decisions",
      Object.entries(current.decisions || {})
        .sort(([a], [b]) => b.localeCompare(a))
        .map(([id, decision]) => [
          id,
          decision.task,
          decision.class,
          decision.overdue ? "overdue" : decision.status,
          decision.neededBy,
          decision.title,
        ]),
      6,
      3,
    );
    const queue = [];
    for (const [project, lanes] of Object.entries(current.applyQueue || {}))
      for (const [lane, names] of Object.entries(lanes))
        queue.push([project, "queue · " + lane, names.join(", ")]);
    for (const heads of Object.values(current.heads || {}))
      for (const [path, head] of Object.entries(heads))
        queue.push([path, head.branch || "detached", head.sha]);
    rows("board-queue", queue, 3);
    rows(
      "board-events",
      [...board.recent]
        .reverse()
        .map(([stamp, ...cells]) => [stamp ? time(stamp / 1000) : null, ...cells]),
      5,
      1,
    );
    text(
      "board-zone",
      `Times are local (${zone()}); a date is shown for any other day.`,
    );
    text("board-cursor", "Event " + shown(board.last));
  }
  async function stream() {
    const headers = { Authorization: "Bearer " + token };
    if (board.last) headers["Last-Event-ID"] = board.last;
    try {
      const response = await fetch("/api/events", {
        headers,
        cache: "no-store",
      });
      if (!response.ok) throw new Error(String(response.status));
      text("board-status", "LIVE");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let end;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, end).split("\n");
          buffer = buffer.slice(end + 2);
          const id = block.find((line) => line.startsWith("id: "));
          const data = block.find((line) => line.startsWith("data: "));
          if (id && data) fold(id.slice(4), JSON.parse(data.slice(6)));
        }
        renderBoard();
      }
      text("board-status", "RECONNECTING");
    } catch (error) {
      if (error.message === "patch") {
        // A delta without its base: drop the cursor and take a fresh snapshot.
        board.state = null;
        board.last = null;
      }
      text(
        "board-status",
        error.message === "401" ? "ACCESS NEEDED" : "WAITING FOR BRIDGE",
      );
    }
    setTimeout(stream, 2000);
  }
  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").querySelector("span:last-child").textContent = state.paused
      ? "Resume"
      : "Pause";
    $("pause").querySelector("span:first-child").textContent = state.paused
      ? "▷"
      : "Ⅱ";
    clearTimeout(state.timer);
    if (state.paused) {
      connection("View paused", "waiting");
      text("last-updated", "Paused · event history is preserved");
    } else poll();
  });
  $("debug-toggle").addEventListener("click", () => {
    state.debug = !state.debug;
    updateDebug();
  });
  for (const [id, change] of [
    ["board-window", () => Object.assign(board, { history: false, page: 0 })],
    ["board-all", () => Object.assign(board, { history: true, page: 0 })],
    ["board-newer", () => (board.page = Math.max(0, board.page - 1))],
    ["board-older", () => (board.page += 1)],
  ])
    $(id).addEventListener("click", () => {
      if (board.pageRequest) board.pageRequest.abort();
      board.pageRequest = null;
      board.pageData = null;
      board.pageError = null;
      change();
      renderBoard();
    });
  $("search").addEventListener("input", renderEvents);
  $("filter").addEventListener("change", renderEvents);
  window.addEventListener("resize", () =>
    chart(state.frame?.stats?.activity || [], state.frame?.generated_at),
  );
  async function start() {
    if (ticket) {
      try {
        const response = await fetch("/api/session", {
          headers: { Authorization: "Ticket " + ticket },
          cache: "no-store",
          signal: AbortSignal.timeout(5000),
        });
        if (response.ok) {
          token = (await response.json()).token;
          sessionStorage.setItem("agent-bus-monitor-token", token);
        }
      } catch (error) {
        // A spent or expired ticket leaves this tab with what it already holds.
      }
    }
    if (token) {
      poll();
      stream();
    } else {
      text("board-status", "ACCESS NEEDED");
      connection("Access needed", "error");
      notice(
        "Open the private URL printed by agent-bus monitor --web to access this session.",
      );
    }
  }
  start();
})();
