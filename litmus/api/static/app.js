/* Litmus dashboard - vanilla JS, no build step.
 *
 * Data flow:
 *   GET  /v1/runs                     run list (polled)
 *   GET  /v1/runs/{id}                status + counts
 *   GET  /v1/runs/{id}/events         SSE over the events table
 *   GET  /v1/runs/{id}/report         authoritative lanes + evidence URLs
 *
 * The SSE endpoint is asked for from_seq=0 on mount, so a refreshed tab
 * replays every persisted event. Reconnects resume from the last seq instead
 * of replaying, which is what keeps the feed duplicate-free.
 */
(() => {
  "use strict";

  const boot = window.LITMUS_BOOTSTRAP || { runs: [], selected: "" };
  const PHASES = [
    "queued",
    "provisioning",
    "installing",
    "exploring",
    "detecting",
    "verifying",
    "reporting",
    "done",
  ];
  const RUNNING = new Set(PHASES.slice(0, 7));
  const EVENT_TYPES = [
    "run.status",
    "step",
    "finding.candidate",
    "verdict",
    "warning",
    "report.ready",
    "done",
  ];
  const ARTIFACT_LABELS = {
    "screen_at_bug.png": "screenshot",
    "screen_before.png": "before",
    "repro.py": "repro.py",
    "logcat.txt": "logcat",
    "logcat_full.txt": "logcat full",
    "trace.json": "trace.json",
    "manifest.json": "manifest",
  };

  const state = {
    runs: boot.runs || [],
    selected: boot.selected || null,
    phase: "",
    phaseIndex: -1,
    steps: 0,
    seen: new Set(),
    cursor: 0,
    stream: null,
    closed: false,
    reconnectTimer: null,
    autoScroll: true,
    reportLoaded: false,
    candidates: new Map(),
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  async function api(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) {
        /* non-JSON error body */
      }
      throw new Error(detail);
    }
    return response.json();
  }

  // ---- runs pane --------------------------------------------------------
  function relativeTime(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
  }

  function renderRuns() {
    const list = $("run-list");
    list.innerHTML = "";
    const empty = $("runs-empty");
    if (empty) empty.hidden = state.runs.length > 0;
    $("run-count").textContent = String(state.runs.length);

    state.runs.forEach((run) => {
      const item = el("li", "run-item");
      if (run.id === state.selected) item.classList.add("is-active");
      item.dataset.runId = run.id;
      const dotClass = RUNNING.has(run.status) ? "running" : run.status;
      item.appendChild(el("span", `run-dot ${dotClass}`));
      const main = el("span", "run-main");
      main.appendChild(el("span", "run-pkg", run.package || "unknown package"));
      // Runs persisted before the state machine settled on "done" still say
      // "completed"; show one vocabulary.
      const status = run.status === "completed" ? "done" : run.status;
      const meta = [status, relativeTime(run.created_at)];
      if (run.steps_taken) meta.push(`${run.steps_taken} steps`);
      main.appendChild(el("span", "run-meta", meta.filter(Boolean).join(" · ")));
      item.appendChild(main);
      if (run.confirmed) item.appendChild(el("span", "run-confirmed", `${run.confirmed}✓`));
      item.addEventListener("click", () => selectRun(run.id));
      list.appendChild(item);
    });
  }

  async function refreshRuns() {
    try {
      const data = await api("/v1/runs?limit=30");
      state.runs = data.runs || [];
      renderRuns();
    } catch (error) {
      console.warn("run list refresh failed:", error.message);
    }
  }

  // ---- phase stepper ----------------------------------------------------
  function setPhase(phase) {
    if (!phase) return;
    if (phase === "completed") phase = "done";
    state.phase = phase;
    const index = PHASES.indexOf(phase);
    if (index >= 0) state.phaseIndex = Math.max(state.phaseIndex, index);
    renderStepper();
  }

  function renderStepper() {
    const stepper = $("stepper");
    stepper.innerHTML = "";
    if (!state.selected) {
      stepper.hidden = true;
      return;
    }
    stepper.hidden = false;
    const terminal = state.phase === "failed" || state.phase === "cancelled";
    PHASES.forEach((phase, index) => {
      const classes = ["phase"];
      if (terminal) {
        if (index <= state.phaseIndex) classes.push("is-done");
      } else if (index === state.phaseIndex) {
        classes.push(phase === "done" ? "is-done" : "is-active");
      } else if (state.phaseIndex > index) {
        classes.push("is-done");
      }
      stepper.appendChild(el("span", classes.join(" "), phase));
    });
    if (terminal) stepper.appendChild(el("span", "phase is-failed", state.phase));
  }

  // ---- live step feed ---------------------------------------------------
  function showFeedEmpty(show) {
    const empty = $("feed-empty");
    if (empty) empty.hidden = !show;
  }

  function addStep(data, seq) {
    showFeedEmpty(false);
    const crash = data.result === "crash";
    const row = el("div", crash ? "feed-row is-crash" : "feed-row");
    if (seq) row.dataset.seq = String(seq);
    row.appendChild(el("span", "step-idx", String(data.idx || 0).padStart(2, "0")));
    row.appendChild(el("span", crash ? "step-kind kind-crash" : "step-kind", data.action || "action"));
    row.appendChild(el("span", "step-target", data.target || ""));
    row.appendChild(el("span", "step-reason", data.reason || ""));
    let resultText = data.changed ? "changed" : "unchanged";
    let resultClass = resultText;
    if (crash) {
      resultText = "CRASH";
      resultClass = "crash";
    } else if (data.result && data.result !== "ok") {
      resultText = "error";
      resultClass = "error";
    }
    row.appendChild(el("span", `step-result ${resultClass}`, resultText));
    $("feed").appendChild(row);
    state.steps += 1;
    $("step-count").textContent = String(state.steps);
    if (state.autoScroll) {
      const body = $("feed-body");
      body.scrollTop = body.scrollHeight;
    }
  }

  function addNote(message, seq) {
    showFeedEmpty(false);
    const note = el("div", "feed-note", message);
    if (seq) note.dataset.seq = String(seq);
    $("feed").appendChild(note);
    if (state.autoScroll) {
      const body = $("feed-body");
      body.scrollTop = body.scrollHeight;
    }
  }

  // ---- findings pane ----------------------------------------------------
  function showReportEmpty(show) {
    const empty = $("report-empty");
    if (empty) empty.hidden = !show;
  }

  function candidateCard(finding) {
    const card = el("article", "finding-card");
    card.dataset.findingId = finding.id || "";
    const top = el("div", "finding-top");
    top.appendChild(el("span", `chip ${finding.confidence || "heuristic"}`, finding.confidence || "heuristic"));
    top.appendChild(el("span", "chip kind", finding.kind || "finding"));
    top.appendChild(el("span", "finding-rate", "awaiting replay"));
    card.appendChild(top);
    card.appendChild(el("p", "finding-sig", finding.signature || ""));
    const meta = [];
    if (finding.screen) meta.push(finding.screen);
    if (finding.step_idx !== undefined && finding.step_idx !== null) meta.push(`step ${finding.step_idx + 1}`);
    card.appendChild(el("div", "finding-meta", meta.join(" · ") || "observed during explore"));
    const attempts = el("div", "finding-links");
    attempts.dataset.role = "attempts";
    card.appendChild(attempts);
    return card;
  }

  function addCandidate(data) {
    const finding = data.finding || {};
    showReportEmpty(false);
    if (state.candidates.has(finding.id)) return;
    const card = candidateCard(finding);
    state.candidates.set(finding.id, card);
    const root = $("report");
    const list = root.querySelector("[data-role='candidates']");
    const bucket = list || el("div", "lane");
    if (!list) {
      bucket.dataset.role = "candidates";
      bucket.appendChild(el("div", "lane-head", "Candidates · replaying"));
      root.prepend(bucket);
    }
    bucket.appendChild(card);
    $("finding-count").textContent = String(state.candidates.size);
  }

  function updateVerdict(data) {
    const card = state.candidates.get(data.finding_id);
    if (!card) return;
    if (typeof data.attempt === "number") {
      const attempts = card.querySelector("[data-role='attempts']");
      if (!attempts) return;
      const label = data.reproduced ? `${data.attempt} ✓` : data.valid ? `${data.attempt} –` : `${data.attempt} ×`;
      const chip = el("span", `chip ${data.reproduced ? "hard" : "heuristic"}`, label);
      chip.title = data.reason || "";
      attempts.appendChild(chip);
    } else if (data.verdict) {
      const rate = card.querySelector(".finding-rate");
      if (rate) rate.textContent = data.summary || data.verdict;
      if (data.verdict === "confirmed") card.style.borderColor = "rgba(52, 211, 153, 0.45)";
      if (data.verdict === "flaky") card.classList.add("is-flaky");
      if (data.verdict === "filtered") card.classList.add("is-filtered");
    }
  }

  function renderLane(name, entries, className) {
    const lane = el("section", "lane");
    const head = el("div", "lane-head");
    head.appendChild(el("span", "", name));
    head.appendChild(el("span", "lane-count", String(entries.length)));
    lane.appendChild(head);
    if (!entries.length) {
      lane.appendChild(el("div", "finding-meta", "none"));
      return lane;
    }
    entries.forEach((entry) => {
      const card = el("article", `finding-card ${className}`);
      const top = el("div", "finding-top");
      top.appendChild(el("span", `chip ${entry.confidence}`, entry.confidence));
      top.appendChild(el("span", "chip kind", entry.kind));
      top.appendChild(el("span", "finding-rate", entry.repro_rate));
      card.appendChild(top);
      card.appendChild(el("p", "finding-sig", entry.signature));
      const meta = [];
      if (entry.screen) meta.push(entry.screen);
      if (entry.step_idx !== null && entry.step_idx !== undefined) meta.push(`step ${entry.step_idx + 1}`);
      if (entry.severity) meta.push(entry.severity);
      card.appendChild(el("div", "finding-meta", meta.join(" · ")));

      const links = el("div", "finding-links");
      const order = ["screen_at_bug.png", "repro.py", "logcat.txt", "trace.json", "manifest.json", "screen_before.png", "logcat_full.txt"];
      const artifacts = (entry.artifacts || []).slice().sort(
        (a, b) => order.indexOf(a.kind) - order.indexOf(b.kind)
      );
      artifacts.forEach((artifact) => {
        if (!artifact.url) return;
        const link = el("a", "", ARTIFACT_LABELS[artifact.kind] || artifact.kind);
        link.href = artifact.url;
        link.target = "_blank";
        link.rel = "noopener";
        links.appendChild(link);
      });
      const detail = el("a", "", "finding json");
      detail.href = `/v1/findings/${encodeURIComponent(entry.id)}`;
      detail.target = "_blank";
      detail.rel = "noopener";
      links.appendChild(detail);
      card.appendChild(links);
      lane.appendChild(card);
    });
    return lane;
  }

  function renderReport(report) {
    const root = $("report");
    root.innerHTML = "";
    state.candidates.clear();
    showReportEmpty(false);
    $("finding-count").textContent = String(report.summary.confirmed);
    $("report-json").hidden = false;
    $("report-json").href = `/v1/runs/${encodeURIComponent(state.selected)}/report`;
    $("open-report").disabled = false;

    const summary = el("div", "summary-grid");
    const stats = [
      ["steps", report.summary.steps, ""],
      ["confirmed", report.summary.confirmed, "accent"],
      ["flaky", report.summary.flaky, ""],
      ["seconds", Math.round(report.summary.duration_s), ""],
    ];
    stats.forEach(([label, value, accent]) => {
      const box = el("div", "stat");
      box.appendChild(el("div", "stat-label", label));
      box.appendChild(el("div", `stat-value ${accent}`.trim(), value));
      summary.appendChild(box);
    });
    root.appendChild(summary);

    root.appendChild(renderLane("Confirmed", report.confirmed, ""));
    root.appendChild(renderLane("Flaky · dropped", report.flaky, "is-flaky"));
    if (report.filtered && report.filtered.length) {
      root.appendChild(renderLane("Filtered · environment", report.filtered, "is-filtered"));
    }
    const note = el("p", "finding-meta", report.confidence_note || "");
    root.appendChild(note);
  }

  async function loadReport() {
    if (!state.selected) return;
    try {
      const report = await api(`/v1/runs/${encodeURIComponent(state.selected)}/report`);
      state.reportLoaded = true;
      renderReport(report);
    } catch (error) {
      console.warn("report load failed:", error.message);
    }
  }

  // ---- SSE --------------------------------------------------------------
  function closeStream() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
  }

  function connect(runId, cursor) {
    const url = `/v1/runs/${encodeURIComponent(runId)}/events?from_seq=${cursor}`;
    const source = new EventSource(url);
    state.stream = source;

    EVENT_TYPES.forEach((type) => {
      source.addEventListener(type, (event) => {
        if (state.selected !== runId) return;
        const seq = Number(event.lastEventId || 0);
        if (seq) {
          if (state.seen.has(seq)) return;
          state.seen.add(seq);
          state.cursor = Math.max(state.cursor, seq);
        }
        let data = {};
        try {
          data = JSON.parse(event.data || "{}");
        } catch (_) {
          data = {};
        }
        handleEvent(type, data, seq);
      });
    });

    source.onerror = () => {
      source.close();
      if (state.closed || state.selected !== runId) return;
      // Reconnect from the last committed seq: from_seq=0 is for a fresh mount.
      state.reconnectTimer = setTimeout(() => connect(runId, state.cursor), 1200);
    };
  }

  function handleEvent(type, data, seq) {
    switch (type) {
      case "run.status":
        setPhase(data.status);
        break;
      case "step":
        addStep(data, seq);
        break;
      case "finding.candidate":
        addCandidate(data);
        if (state.phase === "exploring") setPhase("detecting");
        break;
      case "verdict":
        updateVerdict(data);
        break;
      case "warning":
        addNote(data.message || "warning", seq);
        break;
      case "report.ready":
        loadReport();
        break;
      case "done": {
        state.closed = true;
        closeStream();
        setPhase(data.status || "done");
        const parts = [data.status];
        if (data.steps_taken !== undefined) parts.push(`${data.steps_taken} steps`);
        if (data.duration_s !== undefined) parts.push(`${Math.round(data.duration_s)}s`);
        if (data.error) parts.push(data.error);
        addNote(parts.filter(Boolean).join(" · "), seq);
        loadReport();
        refreshRuns();
        break;
      }
      default:
        break;
    }
  }

  function openStream(runId) {
    state.closed = false;
    state.seen = new Set();
    state.cursor = 0;
    closeStream();
    connect(runId, 0);
  }

  async function selectRun(runId) {
    state.selected = runId;
    state.phase = "";
    state.phaseIndex = -1;
    state.steps = 0;
    state.reportLoaded = false;
    state.candidates.clear();
    $("feed").innerHTML = "";
    $("report").innerHTML = "";
    $("step-count").textContent = "0";
    $("finding-count").textContent = "0";
    $("open-report").disabled = true;
    $("report-json").hidden = true;
    showFeedEmpty(true);
    showReportEmpty(true);
    renderRuns();
    renderStepper();
    openStream(runId);

    try {
      const data = await api(`/v1/runs/${encodeURIComponent(runId)}`);
      if (data.run && data.run.status !== "queued") {
        // A finished run needs no live tail; the report is the whole story.
        if (["done", "failed", "cancelled"].includes(data.run.status)) {
          state.closed = true;
        }
      }
    } catch (_) {
      /* the SSE stream reports its own errors */
    }
  }

  // ---- new run ----------------------------------------------------------
  async function startRun(event) {
    event.preventDefault();
    const fileInput = $("apk");
    const file = fileInput.files && fileInput.files[0];
    const status = $("form-status");
    if (!file) {
      status.textContent = "choose an APK first";
      return;
    }
    const button = $("start-run");
    button.disabled = true;
    status.textContent = "uploading…";
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("profile", $("profile").value);
      const target = await api("/v1/targets", { method: "POST", body: form });
      status.textContent = `target ${target.id} · ${target.package || "unknown package"}`;
      const body = {
        target_id: target.id,
        profile: $("profile").value,
        steps: Number($("steps").value) || 25,
        verify: $("verify").checked,
      };
      const created = await api("/v1/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      status.textContent = `run ${created.run.id} queued (${created.dispatch})`;
      await refreshRuns();
      await selectRun(created.run.id);
    } catch (error) {
      status.textContent = `error: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  // ---- health -----------------------------------------------------------
  async function refreshHealth() {
    try {
      const health = await api("/healthz");
      ["db", "redis", "worker"].forEach((key) => {
        const node = $(`health-${key}`);
        if (!node) return;
        node.className = health[key] === "up" ? "up" : health[key] === "down" ? "down" : "";
        node.textContent = "";
        node.appendChild(el("span", "dot"));
        node.appendChild(document.createTextNode(`${key} ${health[key]}`));
      });
    } catch (_) {
      /* leave the previous health state */
    }
  }

  // ---- wiring -----------------------------------------------------------
  $("run-form").addEventListener("submit", startRun);
  $("refresh-runs").addEventListener("click", refreshRuns);
  $("autoscroll").addEventListener("change", (event) => {
    state.autoScroll = event.target.checked;
  });
  $("open-report").addEventListener("click", () => {
    loadReport();
    $("pane-report").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  renderRuns();
  renderStepper();
  refreshHealth();
  if (state.selected) {
    selectRun(state.selected);
  } else if (state.runs.length) {
    selectRun(state.runs[0].id);
  }

  setInterval(() => {
    if (!document.hidden) refreshRuns();
  }, 4000);
  setInterval(() => {
    if (!document.hidden) refreshHealth();
  }, 10000);
})();