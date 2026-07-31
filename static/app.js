// --- CONSTANTS & DOM REFERENCES ---
const API_BASE = "";
let activeMacrocycleId = null;
let activeMesocycleId = null;
let activeGoalId = null;
const loadedTabs = new Set(["view-dashboard"]);

// Logging helper
function logConsole(message, type = "success") {
    const consoleLogs = document.getElementById("console-logs");
    if (!consoleLogs) return;

    const timestamp = new Date().toLocaleTimeString();
    const line = document.createElement("div");
    line.className = `log-line ${type}`;
    line.innerText = `[${timestamp}] ${message}`;
    consoleLogs.appendChild(line);
    consoleLogs.scrollTop = consoleLogs.scrollHeight;
}

function escapeHtml(s) {
    const div = document.createElement("div");
    div.innerText = s == null ? "" : s;
    return div.innerHTML;
}

function parseLocalDate(dateStr) {
    const parts = dateStr.split("-");
    return new Date(parts[0], parts[1] - 1, parts[2]);
}

function todayStr() {
    return new Date().toISOString().split("T")[0];
}

function sportLabel(sport) {
    return (sport || "").replace(/_/g, " ").toUpperCase();
}

// --- TAB NAVIGATION ---

window.switchMainTab = function(viewId) {
    document.querySelectorAll(".main-view").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".main-tab-btn").forEach(el => el.classList.remove("active"));
    const view = document.getElementById(viewId);
    if (view) view.classList.add("active");
    const btn = document.querySelector(`.main-tab-btn[data-tab="${viewId}"]`);
    if (btn) btn.classList.add("active");

    // Lazy-load each tab's data the first time it is shown.
    if (!loadedTabs.has(viewId)) {
        loadedTabs.add(viewId);
        if (viewId === "view-workouts") fetchWorkouts();
        else if (viewId === "view-learnings") fetchLearningsFull();
        else if (viewId === "view-history") fetchHistory();
        else if (viewId === "view-progress") fetchProgress();
    }
};

// Switch tabs for objectives vs life events (dashboard accordion)
window.switchTab = function(tabId) {
    document.querySelectorAll(".tab-content").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".accordion-card .tab-btn").forEach(el => el.classList.remove("active"));

    document.getElementById(tabId).classList.add("active");
    const btn = Array.from(document.querySelectorAll(".accordion-card .tab-btn")).find(
        b => (tabId === "tab-goals" && b.innerText.includes("Objectives")) ||
             (tabId === "tab-events" && b.innerText.includes("Life Events"))
    );
    if (btn) btn.classList.add("active");
};

// --- MODAL HELPER ---

window.closeModal = function() {
    const overlay = document.getElementById("modal-overlay");
    if (overlay) overlay.style.display = "none";
    document.getElementById("modal-body").innerHTML = "";
};

function openModal(title, fields, onSubmit, submitLabel = "Save") {
    // fields: [{id, label, type, value, options?, required?, placeholder?}]
    document.getElementById("modal-title").innerText = title;
    const body = document.getElementById("modal-body");
    body.innerHTML = "";
    const form = document.createElement("form");
    form.className = "add-form";
    fields.forEach(f => {
        const wrap = document.createElement("div");
        wrap.className = "form-field";
        const label = document.createElement("label");
        label.innerText = f.label;
        wrap.appendChild(label);
        let input;
        if (f.type === "select") {
            input = document.createElement("select");
            (f.options || []).forEach(o => {
                const opt = document.createElement("option");
                opt.value = o.value;
                opt.innerText = o.label;
                if (o.value === f.value) opt.selected = true;
                input.appendChild(opt);
            });
        } else if (f.type === "textarea") {
            input = document.createElement("textarea");
            input.value = f.value || "";
        } else {
            input = document.createElement("input");
            input.type = f.type || "text";
            if (f.value != null) input.value = f.value;
        }
        input.id = `modal-field-${f.id}`;
        if (f.required) input.required = true;
        if (f.placeholder) input.placeholder = f.placeholder;
        wrap.appendChild(input);
        form.appendChild(wrap);
    });
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "btn btn-primary btn-sm";
    submit.innerHTML = `<i class="fa-solid fa-save"></i> ${submitLabel}`;
    form.appendChild(submit);

    form.addEventListener("submit", (e) => {
        e.preventDefault();
        const values = {};
        fields.forEach(f => {
            values[f.id] = document.getElementById(`modal-field-${f.id}`).value;
        });
        onSubmit(values);
    });

    body.appendChild(form);
    document.getElementById("modal-overlay").style.display = "flex";
}

// --- STATUS (dashboard) ---

async function fetchStatus() {
    try {
        const res = await fetch(`${API_BASE}/api/status`);
        if (!res.ok) throw new Error("Failed to load status");
        const data = await res.json();

        const goalTitleEl = document.getElementById("header-goal-title");
        const goalCountdownEl = document.getElementById("header-goal-countdown");
        if (data.next_goal) {
            activeGoalId = data.next_goal.id;
            const sportsList = data.next_goal.sport_type
                .split(',').map(s => sportLabel(s.trim())).join(', ');
            goalTitleEl.innerText = `${data.next_goal.title} (${sportsList})`;

            const target = parseLocalDate(data.next_goal.target_date);
            const today = new Date();
            const diffDays = Math.ceil((target - today) / (1000 * 60 * 60 * 24));
            if (diffDays > 0) {
                goalCountdownEl.innerText = `${diffDays} days remaining`;
                goalCountdownEl.style.color = "var(--accent-cyan)";
            } else if (diffDays === 0) {
                goalCountdownEl.innerText = `Today is the day!`;
                goalCountdownEl.style.color = "var(--accent-green)";
            } else {
                goalCountdownEl.innerText = `Completed`;
                goalCountdownEl.style.color = "var(--text-muted)";
            }
        } else {
            activeGoalId = null;
            goalTitleEl.innerText = "No Active Goal";
            goalCountdownEl.innerText = "Add a goal to start planning";
            goalCountdownEl.style.color = "var(--text-muted)";
        }

        updateMetrics(data.last_metrics, data.last_baseline);
        renderLearningsSummary(
            (data.coach_learnings && data.coach_learnings.learnings) || [],
            (data.coach_learnings && data.coach_learnings.summary) || null
        );
        renderSyncFreshness(data.sync_state);

        const strategyCard = document.getElementById("strategy-card");
        const noStrategyCard = document.getElementById("no-strategy-card");
        if (data.macrocycle && data.mesocycles && data.mesocycles.length > 0) {
            activeMacrocycleId = data.macrocycle.id;
            strategyCard.style.display = "block";
            if (noStrategyCard) noStrategyCard.style.display = "none";
            document.getElementById("strategy-philosophy").innerText =
                data.macrocycle.strategy;
            renderStrategyInputs(data.macrocycle);
            document.getElementById("macro-feedback-input").value =
                data.macrocycle.feedback || "";
            document.getElementById("macro-feedback-notice").style.display = "none";
            renderTimeline(data.mesocycles);

            if (data.macrocycle.created_at) {
                const created = new Date(data.macrocycle.created_at);
                const formattedDate = created.toLocaleDateString("en-US", {
                    month: "short",
                    day: "numeric",
                    year: "numeric"
                });
                const formattedTime = created.toLocaleTimeString("en-US", {
                    hour: "numeric",
                    minute: "2-digit"
                });
                const generatedText = `Generated ${formattedDate} at ${formattedTime}`;
                document.getElementById("strategy-generated-at").innerText = generatedText;
            } else {
                document.getElementById("strategy-generated-at").innerText = "";
            }
        } else {
            activeMacrocycleId = null;
            strategyCard.style.display = "none";
            if (noStrategyCard) noStrategyCard.style.display = "block";
        }

        const banner = document.getElementById("config-warning-banner");
        if (banner) {
            if (data.config_mismatch) {
                banner.style.display = "flex";
                logConsole(
                    "Warning: config.yaml has changed since the active periodization " +
                    "plan was generated. Run 'Generate Plan' to update.", "warning"
                );
            } else {
                banner.style.display = "none";
            }
        }
    } catch (e) {
        logConsole(`Error fetching status: ${e.message}`, "error");
    }
}

function renderLearningsSummary(learnings, summary) {
    const el = document.getElementById("memory-learnings");
    if (!el) return;
    if (!learnings || learnings.length === 0) {
        el.innerText = "No observations cached yet. Run 'data bootstrap' (CLI) to "
            + "reconstruct your training history and seed observations.";
        return;
    }
    let txt = `${summary ? summary.active : learnings.length} active observation(s)`;
    if (summary && summary.dormant) txt += ` · ${summary.dormant} dormant`;
    if (summary && summary.pending_demotion) txt += ` · ${summary.pending_demotion} pending demotion`;
    el.innerText = txt;
}

function renderSyncFreshness(syncState) {
    const el = document.getElementById("sync-freshness");
    if (!el) return;
    if (!syncState || (!syncState.through_date && !syncState.last_pull_utc)) {
        el.innerText = "Garmin data: never pulled — run 'data pull' (CLI).";
        return;
    }
    const through = syncState.through_date || "?";
    let ago = "";
    if (syncState.last_pull_utc) {
        const last = new Date(syncState.last_pull_utc);
        const hours = Math.floor((Date.now() - last) / 3600000);
        ago = hours < 1 ? " (just now)"
            : hours < 24 ? ` (${hours}h ago)`
            : ` (${Math.floor(hours / 24)}d ago)`;
    }
    el.innerText = `Garmin data through ${through}${ago} — pulling is CLI-only.`;
}

// Renders the goals and life events the plan was generated from. These are
// snapshotted on the macrocycle (server-side), so they reflect the inputs the plan
// was built on rather than the current live records, which may since have changed.
// Older plans predate the snapshot (null fields) and show a brief note instead.
function renderStrategyInputs(macrocycle) {
    const el = document.getElementById("strategy-inputs");
    if (!el) return;

    const rawGoals = macrocycle.goals_snapshot;
    // New snapshots carry constraint fields; legacy plans (pre-constraints-rename)
    // carry the old lifeevents_snapshot — read whichever is present.
    const rawEvents = macrocycle.constraints_snapshot ?? macrocycle.lifeevents_snapshot;
    if (rawGoals == null && rawEvents == null) {
        el.innerHTML = `<div class="strategy-inputs-note">`
            + `Inputs considered: not recorded (plan predates input snapshots).</div>`;
        return;
    }

    let goals = [], events = [];
    try { goals = rawGoals ? JSON.parse(rawGoals) : []; } catch (e) { goals = []; }
    try { events = rawEvents ? JSON.parse(rawEvents) : []; } catch (e) { events = []; }

    const goalItems = goals.length
        ? goals.map(g => `<li>`
            + `<span class="si-title">${escapeHtml(g.title || "")}</span> `
            + `<span class="si-meta">(${escapeHtml((g.sport_type || "").toUpperCase())}) `
            + `· ${escapeHtml(g.target_date || "")} · priority ${escapeHtml(String(g.priority ?? ""))}</span>`
            + (g.description ? `<div class="si-desc">${escapeHtml(g.description)}</div>` : "")
            + `</li>`).join("")
        : `<li class="si-empty">None</li>`;

    const eventItems = events.length
        ? events.map(e => {
            // Snapshots are historical: tolerate the current `rest` flag, the pre-rev-6
            // binding/sport/type, and the original event_type/impact_description.
            const label = e.type || e.event_type || "";
            const enforcement = "rest" in e
                ? (e.rest ? "no training" : "advisory")
                : (e.binding || "");
            const tags = [label, enforcement, e.sport ? `[${e.sport}]` : ""]
                .filter(Boolean).map(escapeHtml).join(" ");
            const detail = e.description || e.impact_description;
            return `<li>`
                + `<span class="si-title">${escapeHtml(e.title || "")}</span> `
                + `<span class="si-meta">(${tags}) `
                + `· ${escapeHtml(e.start_date || "")} → ${escapeHtml(e.end_date || "")}</span>`
                + (detail ? `<div class="si-desc">${escapeHtml(detail)}</div>` : "")
                + `</li>`;
        }).join("")
        : `<li class="si-empty">None</li>`;

    el.innerHTML = `
        <details class="strategy-inputs-details">
            <summary>Inputs considered (${goals.length} goal${goals.length === 1 ? "" : "s"}, `
                + `${events.length} constraint${events.length === 1 ? "" : "s"})</summary>
            <div class="si-section">
                <div class="si-heading"><i class="fa-solid fa-flag-checkered"></i> Goals considered</div>
                <ul class="si-list">${goalItems}</ul>
            </div>
            <div class="si-section">
                <div class="si-heading"><i class="fa-solid fa-calendar-day"></i> Constraints considered</div>
                <ul class="si-list">${eventItems}</ul>
            </div>
        </details>`;
}

function renderTimeline(mesocycles) {
    const container = document.getElementById("web-timeline-container");
    if (!container) return;
    container.innerHTML = "";

    const detailsBox = document.getElementById("cycle-details-box");
    const detailsName = document.getElementById("cycle-details-name");
    const detailsDates = document.getElementById("cycle-details-dates");
    const detailsFocus = document.getElementById("cycle-details-focus");

    if (!mesocycles || mesocycles.length === 0) {
        detailsBox.style.display = "none";
        return;
    }

    mesocycles.sort((a, b) => parseLocalDate(a.start_date) - parseLocalDate(b.start_date));
    const overallStart = parseLocalDate(mesocycles[0].start_date);
    const overallEnd = parseLocalDate(mesocycles[mesocycles.length - 1].end_date);
    const totalDays = Math.ceil((overallEnd - overallStart) / (1000 * 60 * 60 * 24)) + 1;

    const today = new Date();
    today.setHours(0, 0, 0, 0);
    let activeBlockEl = null;

    mesocycles.forEach(m => {
        const start = parseLocalDate(m.start_date);
        const end = parseLocalDate(m.end_date);
        const duration = Math.ceil((end - start) / (1000 * 60 * 60 * 24)) + 1;
        const widthPct = totalDays > 0 ? (duration / totalDays) * 100 : 100;

        const block = document.createElement("div");
        block.className = "cycle-block";
        block.style.width = `${widthPct}%`;
        block.innerText = m.name;
        block.title = `${m.name} (${m.start_date} to ${m.end_date})`;

        let status = "future";
        if (end < today) status = "done";
        else if (start <= today && today <= end) status = "active";
        block.classList.add(status);

        block.addEventListener("click", () => {
            document.querySelectorAll(".cycle-block").forEach(el => el.classList.remove("selected"));
            block.classList.add("selected");
            detailsBox.style.display = "block";
            detailsName.innerText = m.name;
            detailsDates.innerText = `${m.start_date} to ${m.end_date} (${duration} days)`;
            detailsFocus.innerText = m.focus;
            activeMesocycleId = m.id;
            document.getElementById("meso-feedback-input").value = m.feedback || "";
            document.getElementById("meso-feedback-notice").style.display = "none";
        });

        container.appendChild(block);
        if (status === "active") activeBlockEl = block;
    });

    const defaultSelect = activeBlockEl || container.firstElementChild;
    if (defaultSelect) defaultSelect.click();
}

function updateMetrics(metrics, baseline) {
    // 1. HRV
    const hrvVal = document.querySelector("#metric-hrv .metric-value");
    const hrvBase = document.querySelector("#metric-hrv .metric-baseline");
    const hrvBadge = document.getElementById("hrv-badge");
    if (metrics && metrics.hrv) {
        hrvVal.innerHTML = `${metrics.hrv} <span class="unit">ms</span>`;
        if (baseline && baseline.hrv_baseline_mean) {
            hrvBase.innerText = `baseline: ${baseline.hrv_baseline_mean.toFixed(1)} ms`;
            const diff = metrics.hrv - baseline.hrv_baseline_mean;
            const std = baseline.hrv_baseline_std || 5.0;
            hrvBadge.style.display = "inline-block";
            if (diff < -1.5 * std) { hrvBadge.className = "status-badge badge badge-danger"; hrvBadge.innerText = "Suppressed"; }
            else if (diff < -1.0 * std) { hrvBadge.className = "status-badge badge badge-warning"; hrvBadge.innerText = "Fatigued"; }
            else { hrvBadge.className = "status-badge badge badge-success"; hrvBadge.innerText = "Balanced"; }
        } else { hrvBase.innerText = "baseline: N/A"; hrvBadge.style.display = "none"; }
    } else { hrvVal.innerText = "--"; hrvBase.innerText = "baseline: --"; hrvBadge.style.display = "none"; }

    // 2. RHR
    const rhrVal = document.querySelector("#metric-rhr .metric-value");
    const rhrBase = document.querySelector("#metric-rhr .metric-baseline");
    const rhrBadge = document.getElementById("rhr-badge");
    if (metrics && metrics.rhr) {
        rhrVal.innerHTML = `${metrics.rhr} <span class="unit">bpm</span>`;
        if (baseline && baseline.rhr_baseline_mean) {
            rhrBase.innerText = `baseline: ${baseline.rhr_baseline_mean.toFixed(1)} bpm`;
            const diff = metrics.rhr - baseline.rhr_baseline_mean;
            const std = baseline.rhr_baseline_std || 2.5;
            rhrBadge.style.display = "inline-block";
            if (diff > 2.0 * std || diff >= 5) { rhrBadge.className = "status-badge badge badge-danger"; rhrBadge.innerText = "Elevated"; }
            else if (diff > 1.0 * std || diff >= 3) { rhrBadge.className = "status-badge badge badge-warning"; rhrBadge.innerText = "Stressed"; }
            else { rhrBadge.className = "status-badge badge badge-success"; rhrBadge.innerText = "Normal"; }
        } else { rhrBase.innerText = "baseline: N/A"; rhrBadge.style.display = "none"; }
    } else { rhrVal.innerText = "--"; rhrBase.innerText = "baseline: --"; rhrBadge.style.display = "none"; }

    // 3. Sleep
    const sleepVal = document.querySelector("#metric-sleep .metric-value");
    const sleepBase = document.querySelector("#metric-sleep .metric-baseline");
    const sleepBadge = document.getElementById("sleep-badge");
    if (metrics && metrics.sleep_score) {
        sleepVal.innerHTML = `${metrics.sleep_score} <span class="unit">/100</span>`;
        if (baseline && baseline.sleep_baseline_mean) {
            sleepBase.innerText = `baseline: ${baseline.sleep_baseline_mean.toFixed(1)}`;
            sleepBadge.style.display = "inline-block";
            if (metrics.sleep_score < 60) { sleepBadge.className = "status-badge badge badge-danger"; sleepBadge.innerText = "Poor"; }
            else if (metrics.sleep_score < 75) { sleepBadge.className = "status-badge badge badge-warning"; sleepBadge.innerText = "Fair"; }
            else { sleepBadge.className = "status-badge badge badge-success"; sleepBadge.innerText = "Good"; }
        } else { sleepBase.innerText = "baseline: N/A"; sleepBadge.style.display = "none"; }
    } else { sleepVal.innerText = "--"; sleepBase.innerText = "baseline: --"; sleepBadge.style.display = "none"; }

    // 4. ATL:CTL — relative overload. Only the high end warns: a LOW ratio is a taper,
    // deload, or intensity block doing its job (training_load.txt §3/§4).
    const ratioVal = document.querySelector("#metric-load-ratio .metric-value");
    const ratioBase = document.querySelector("#metric-load-ratio .metric-baseline");
    const ratioBadge = document.getElementById("load-ratio-badge");
    const ratio = (metrics && metrics.ctl > 0 && metrics.atl != null)
        ? metrics.atl / metrics.ctl : null;
    if (ratio !== null) {
        ratioVal.innerText = ratio.toFixed(2);
        ratioBase.innerText = `fatigue: ${metrics.atl.toFixed(0)} | fitness: ${metrics.ctl.toFixed(0)}`;
        ratioBadge.style.display = "inline-block";
        if (ratio > 1.5) { ratioBadge.className = "status-badge badge badge-danger"; ratioBadge.innerText = "Spike"; }
        else if (ratio > 1.3) { ratioBadge.className = "status-badge badge badge-warning"; ratioBadge.innerText = "Overload"; }
        else if (ratio >= 1.0) { ratioBadge.className = "status-badge badge badge-success"; ratioBadge.innerText = "Building"; }
        else { ratioBadge.className = "status-badge badge badge-info"; ratioBadge.innerText = "Unloading"; }
    } else { ratioVal.innerText = "--"; ratioBase.innerText = "fatigue: -- | fitness: --"; ratioBadge.style.display = "none"; }
}

// --- OBJECTIVES ---

async function fetchObjectives() {
    try {
        const res = await fetch(`${API_BASE}/api/objectives`);
        if (!res.ok) throw new Error();
        const goals = await res.json();
        const container = document.getElementById("goals-list");
        container.innerHTML = "";
        if (goals.length === 0) {
            container.innerHTML = `<div class="item-meta" style="padding:0.5rem;">No goals. Add one below to start planning.</div>`;
            return;
        }
        goals.forEach(g => {
            const sportsList = g.sport_type.split(',').map(s => s.trim().replace(/_/g, ' ')).join(', ');
            const item = document.createElement("div");
            item.className = "list-item";
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-title">${escapeHtml(g.title)} (${escapeHtml(sportsList)})</span>
                    <span class="item-meta">Target: ${g.target_date} | Priority: ${g.priority} | Status: ${g.status}</span>
                </div>
                <div class="item-actions">
                    <button class="btn-icon-only" title="Edit" onclick="editObjective(${g.id})"><i class="fa-solid fa-pen"></i></button>
                    <button class="btn-icon-only" title="Delete" onclick="deleteObjective(${g.id})"><i class="fa-solid fa-trash-can"></i></button>
                </div>`;
            container.appendChild(item);
            item._goal = g;
        });
        window._goalsCache = goals;
    } catch (e) {
        logConsole("Failed to load goals", "error");
    }
}

window.editObjective = function(id) {
    const g = (window._goalsCache || []).find(x => x.id === id);
    if (!g) return;
    openModal("Edit Objective", [
        { id: "title", label: "Title", value: g.title, required: true },
        { id: "target_date", label: "Target Date", type: "date", value: g.target_date, required: true },
        { id: "sport_type", label: "Sports (comma-separated)", value: g.sport_type, required: true },
        { id: "priority", label: "Priority", type: "number", value: g.priority },
        { id: "description", label: "Description", value: g.description || "" },
        { id: "status", label: "Status", type: "select", value: g.status, options: [
            { value: "active", label: "active" },
            { value: "completed", label: "completed" },
            { value: "archived", label: "archived" },
        ]},
    ], async (vals) => {
        vals.priority = parseInt(vals.priority, 10) || 1;
        const res = await fetch(`${API_BASE}/api/objectives/${id}`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(vals),
        });
        const data = await res.json();
        if (res.ok) { logConsole(data.message || "Objective updated."); closeModal(); fetchObjectives(); fetchStatus(); }
        else logConsole(`Update failed: ${data.error}`, "error");
    });
};

window.deleteObjective = async function(id) {
    if (!confirm("Delete this objective?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/objectives/${id}`, { method: "DELETE" });
        if (res.ok) { logConsole("Objective deleted."); fetchObjectives(); fetchStatus(); }
    } catch (e) { logConsole("Failed to delete objective", "error"); }
};

// --- CONSTRAINTS (DESIGN_constraints.md — supersedes life events) ---
// A constraint is advisory prose the coach works around; the one toggle is `rest`, a
// deterministic full no-training window (rev 6). The `type`/`binding`/`sport` fields are
// gone.

async function fetchEvents() {
    try {
        const res = await fetch(`${API_BASE}/api/constraints`);
        if (!res.ok) throw new Error();
        const events = await res.json();
        const container = document.getElementById("events-list");
        container.innerHTML = "";
        if (events.length === 0) {
            container.innerHTML = `<div class="item-meta" style="padding:0.5rem;">No upcoming constraints. Log travel or injuries.</div>`;
            return;
        }
        events.forEach(e => {
            const item = document.createElement("div");
            item.className = "list-item";
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-title">${escapeHtml(e.title)} (${e.rest ? "no training" : "advisory"})</span>
                    <span class="item-meta">${e.start_date} to ${e.end_date}${e.description ? " · " + escapeHtml(e.description) : ""}</span>
                </div>
                <div class="item-actions">
                    <button class="btn-icon-only" title="Edit" onclick="editEvent(${e.id})"><i class="fa-solid fa-pen"></i></button>
                    <button class="btn-icon-only" title="Delete" onclick="deleteEvent(${e.id})"><i class="fa-solid fa-trash-can"></i></button>
                </div>`;
            container.appendChild(item);
        });
        window._eventsCache = events;
    } catch (e) { logConsole("Failed to load events", "error"); }
}

window.editEvent = function(id) {
    const ev = (window._eventsCache || []).find(x => x.id === id);
    if (!ev) return;
    openModal("Edit Constraint", [
        { id: "title", label: "Title", value: ev.title, required: true },
        { id: "rest", label: "Enforcement", type: "select", value: ev.rest ? "1" : "0", options: [
            { value: "0", label: "Advisory (coach works around it)" },
            { value: "1", label: "No training (deterministic rest)" },
        ]},
        { id: "start_date", label: "Start", type: "date", value: ev.start_date, required: true },
        { id: "end_date", label: "End", type: "date", value: ev.end_date, required: true },
        { id: "description", label: "Details for the coach", type: "textarea", value: ev.description || "" },
    ], async (vals) => {
        vals.rest = vals.rest === "1" ? 1 : 0;
        const res = await fetch(`${API_BASE}/api/constraints/${id}`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(vals),
        });
        const data = await res.json();
        if (res.ok) { logConsole(data.message || "Constraint updated."); closeModal(); fetchEvents(); }
        else logConsole(`Update failed: ${data.error}`, "error");
    });
};

window.deleteEvent = async function(id) {
    if (!confirm("Delete this constraint?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/constraints/${id}`, { method: "DELETE" });
        if (res.ok) { logConsole("Constraint deleted."); fetchEvents(); }
    } catch (e) { logConsole("Failed to delete event", "error"); }
};

// --- WORKOUTS ---

const SPORT_ICONS = {
    running: "fa-person-running", road_biking: "fa-bicycle", hiking: "fa-mountain-sun",
    strength_training: "fa-dumbbell", yoga: "fa-spa", ski_touring: "fa-person-skiing-nordic",
    rest: "fa-bed",
};

async function fetchWorkouts() {
    try {
        const from = document.getElementById("wfilter-from").value || todayStr();
        const until = document.getElementById("wfilter-until").value;
        const sport = document.getElementById("wfilter-sport").value;
        const removed = document.getElementById("wfilter-removed").checked;

        const params = new URLSearchParams({ start_date: from });
        if (until) params.set("end_date", until);
        if (sport) params.set("sport_type", sport);
        if (removed) params.set("include_removed", "1");

        const res = await fetch(`${API_BASE}/api/workouts?${params}`);
        if (!res.ok) throw new Error();
        const workouts = await res.json();

        const container = document.getElementById("workouts-list-container");
        container.innerHTML = "";
        document.getElementById("workout-count").innerText = `${workouts.length} workouts`;

        if (workouts.length === 0) {
            container.innerHTML = `<div class="item-meta" style="text-align:center; padding: 2rem;">No workouts in range. Generate a plan or add one manually.</div>`;
            return;
        }
        workouts.forEach(w => container.appendChild(renderWorkoutCard(w)));
    } catch (e) { logConsole("Failed to load workouts", "error"); }
}

function renderWorkoutCard(w) {
    const dateObj = parseLocalDate(w.date);
    const dayNum = dateObj.getDate();
    const monthStr = dateObj.toLocaleDateString("en-US", { month: "short" });
    const weekdayStr = dateObj.toLocaleDateString("en-US", { weekday: "short" });

    // Derived facts come from the API (calendar_status / modification_status),
    // mirroring the CLI markers (ARCHITECTURE.md §5).
    const modStatus = w.modification_status || "unmodified";
    const calStatus = w.calendar_status || "unpushed";
    const isRemoved = !!w.removed;
    const isManual = w.source === "manual";

    let cardClass = "workout-card";
    if (isRemoved) cardClass += " removed";
    else if (modStatus !== "unmodified") cardClass += " adapted";
    else if (calStatus === "synced") cardClass += " synced";

    const iconGlyph = SPORT_ICONS[w.sport_type] || "fa-bed";
    const iconClass = `workout-sport-icon ${w.sport_type || "rest"}`;

    const badges = [];
    if (modStatus === "adapted") badges.push(`<span class="wbadge adapted">ADAPTED</span>`);
    else if (modStatus === "swapped") badges.push(`<span class="wbadge swapped">SWAPPED</span>`);
    else if (modStatus === "replaced") badges.push(`<span class="wbadge replaced">REPLACED</span>`);
    if (isManual) badges.push(`<span class="wbadge manual">MANUAL</span>`);
    if (calStatus === "synced") badges.push(`<span class="wbadge synced">SYNCED</span>`);
    else if (calStatus === "stale") badges.push(`<span class="wbadge stale">STALE</span>`);
    if (isRemoved) badges.push(`<span class="wbadge removed">REMOVED</span>`);

    const stats = [];
    if (w.duration_minutes) stats.push(`${w.duration_minutes}min`);
    if (w.tss != null) stats.push(`TSS ${w.tss}`);
    if (w.rpe != null) stats.push(`RPE ${w.rpe}`);
    const statsHtml = stats.length ? `<span class="workout-stats">${stats.join(" · ")}</span>` : "";

    let reasonHtml = "";
    if (w.modification_reason) {
        reasonHtml = `<div class="workout-reason"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(w.modification_reason)}</div>`;
    }
    let origHtml = "";
    if (w.original_description && w.original_description !== w.description) {
        origHtml = `<div class="workout-orig"><i class="fa-solid fa-clock-rotate-left"></i> Originally: ${escapeHtml(w.original_description)}</div>`;
    }
    let removedHtml = "";
    if (isRemoved && w.removed_reason) {
        removedHtml = `<div class="workout-reason"><i class="fa-solid fa-ban"></i> Cancelled: ${escapeHtml(w.removed_reason)}</div>`;
    }

    const item = document.createElement("div");
    item.className = cardClass;
    item.innerHTML = `
        <div class="workout-date">
            <span class="workout-day">${dayNum}</span>
            <span class="workout-month">${monthStr}</span>
            <span class="workout-weekday">${weekdayStr}</span>
        </div>
        <div class="workout-body">
            <span class="workout-title">${escapeHtml(w.title)} ${badges.join(" ")}</span>
            ${statsHtml}
            <span class="workout-desc">${escapeHtml(w.description)}</span>
            ${reasonHtml}${origHtml}${removedHtml}
        </div>
        <div class="${iconClass}"><i class="fa-solid ${iconGlyph}"></i></div>
        <div class="workout-actions"></div>`;

    // Wire actions programmatically so titles with quotes can't break markup.
    const actionsEl = item.querySelector(".workout-actions");
    if (isRemoved) {
        const restore = document.createElement("button");
        restore.className = "btn-icon-only"; restore.title = "Restore";
        restore.innerHTML = `<i class="fa-solid fa-rotate-left"></i>`;
        restore.addEventListener("click", () => restoreWorkout(w.id));
        actionsEl.appendChild(restore);
    } else {
        const swap = document.createElement("button");
        swap.className = "btn-icon-only"; swap.title = "Swap to another date";
        swap.innerHTML = `<i class="fa-solid fa-right-left"></i>`;
        swap.addEventListener("click", () => openSwapModal(w.id, w.date, w.title));
        const rm = document.createElement("button");
        rm.className = "btn-icon-only"; rm.title = "Remove";
        rm.innerHTML = `<i class="fa-solid fa-xmark"></i>`;
        rm.addEventListener("click", () => removeWorkout(w.id));
        actionsEl.appendChild(swap);
        actionsEl.appendChild(rm);
    }
    return item;
}

window.removeWorkout = async function(id) {
    const reason = prompt("Remove this session? Optionally note why (cancellation reason):");
    if (reason === null) return;
    try {
        const res = await fetch(`${API_BASE}/api/workouts/${id}/remove`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason }),
        });
        const data = await res.json();
        if (res.ok) {
            logConsole(data.message || "Workout removed.");
            if (data.warning) logConsole(`Calendar warning: ${data.warning}`, "warning");
            fetchWorkouts();
        } else logConsole(`Remove failed: ${data.error}`, "error");
    } catch (e) { logConsole(`Remove error: ${e.message}`, "error"); }
};

window.restoreWorkout = async function(id) {
    try {
        const res = await fetch(`${API_BASE}/api/workouts/${id}/restore`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(data.message || "Workout restored.");
            if (data.warning) logConsole(`Calendar warning: ${data.warning}`, "warning");
            fetchWorkouts();
        } else logConsole(`Restore failed: ${data.error}`, "error");
    } catch (e) { logConsole(`Restore error: ${e.message}`, "error"); }
};

window.openSwapModal = function(id, currentDate, title) {
    openModal(`Swap "${title}"`, [
        { id: "new_date", label: "New date for this session", type: "date", value: currentDate, required: true },
        { id: "reason", label: "Reason (required)", value: "", required: true, placeholder: "Why move it?" },
    ], (vals) => submitSwap(id, vals.new_date, vals.reason, false));
};

async function submitSwap(id, newDate, reason, force) {
    try {
        const res = await fetch(`${API_BASE}/api/workouts/swap`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ops: [{ id, new_date: newDate }], reason, force }),
        });
        const data = await res.json();
        if (!res.ok) { logConsole(`Swap failed: ${data.error}`, "error"); return; }
        if (data.applied === false && data.warnings && data.warnings.length) {
            // Validation raised recovery warnings — confirm a forced retry.
            const msg = "Swap warnings:\n- " + data.warnings.join("\n- ") + "\n\nProceed anyway?";
            if (confirm(msg)) { submitSwap(id, newDate, reason, true); }
            else logConsole("Swap cancelled.", "system");
            return;
        }
        logConsole(data.message || "Swap applied.");
        closeModal();
        fetchWorkouts();
    } catch (e) { logConsole(`Swap error: ${e.message}`, "error"); }
}

// --- COMPARE / ADHERENCE ---

async function runCompare() {
    const from = document.getElementById("cmp-from").value;
    const until = document.getElementById("cmp-until").value;
    const sport = document.getElementById("cmp-sport").value;
    const params = new URLSearchParams();
    if (from) params.set("start_date", from);
    if (until) params.set("end_date", until);
    if (sport) params.set("sport", sport);

    const target = document.getElementById("compare-results");
    target.innerHTML = `<div class="item-meta">Comparing...</div>`;
    try {
        const res = await fetch(`${API_BASE}/api/workouts/compare?${params}`);
        const data = await res.json();
        if (!res.ok) { target.innerHTML = `<div class="log-line error">${escapeHtml(data.error)}</div>`; return; }
        renderCompare(data);
    } catch (e) {
        target.innerHTML = `<div class="log-line error">Compare error: ${escapeHtml(e.message)}</div>`;
    }
}

function fmtActivity(u) {
    const a = u.activity;
    const parts = [`${(a.duration_sec / 60).toFixed(0)}min`, `load ${u.load}`];
    if (a.tss) parts.push(`TSS ${Math.round(a.tss)}`);
    if (a.rpe) parts.push(`RPE ${a.rpe}`);
    let s = `[${a.activity_type}] ${a.activity_name} (${parts.join(", ")})`;
    if (u.rpe_divergence) s += ` — HR under-counted ${u.rpe_divergence}x`;
    return s;
}

function renderCompare(data) {
    const target = document.getElementById("compare-results");
    target.innerHTML = "";

    const head = document.createElement("div");
    head.className = "item-meta compare-filters";
    head.innerText = `From ${data.filters.start_date} to ${data.filters.end_date}` +
        (data.filters.sport ? ` · ${data.filters.sport}` : "");
    target.appendChild(head);

    if (!data.days.length) {
        target.insertAdjacentHTML("beforeend", `<div class="item-meta">No planned workouts or completed activities in this range.</div>`);
        return;
    }

    data.days.forEach(day => {
        const block = document.createElement("div");
        block.className = "compare-day";
        let html = `<div class="compare-date">${day.date}</div>`;
        day.results.forEach(r => {
            const w = r.planned;
            const planned = r.is_rest ? `[REST]` :
                `[${sportLabel(w.sport_type)}] <strong>${escapeHtml(w.title)}</strong>` +
                (w.duration_minutes ? ` (${w.duration_minutes}min${w.tss != null ? ", TSS " + w.tss : ""})` : "");
            html += `<div class="compare-line"><span class="compare-label">PLANNED</span> ${planned}</div>`;
            if (r.completed) {
                const a = r.completed;
                const act = `[${a.activity_type}] ${escapeHtml(a.activity_name)} (${(a.duration_sec / 60).toFixed(0)}min${a.tss ? ", TSS " + Math.round(a.tss) : ""})`;
                const cls = r.rest_violation ? "actual-bad" : "actual-good";
                const tag = r.rest_violation ? ` <strong class="actual-bad">[REST VIOLATION]</strong>` : "";
                html += `<div class="compare-line"><span class="compare-label">ACTUAL</span> <span class="${cls}">${act}</span>${tag}</div>`;
            } else if (!r.is_rest) {
                html += `<div class="compare-line"><span class="compare-label">ACTUAL</span> <span class="actual-bad">(none — missed)</span></div>`;
            }
        });
        day.unplanned.forEach(u => {
            const label = u.kind === "minor" ? "(minor)" : u.kind === "off_plan" ? "(off-plan)" : "UNPLANNED";
            const cls = u.kind === "unplanned" ? "actual-warn" : "actual-muted";
            html += `<div class="compare-line"><span class="compare-label">${label}</span> <span class="${cls}">${escapeHtml(fmtActivity(u))}</span></div>`;
        });
        block.innerHTML = html;
        target.appendChild(block);
    });

    const summary = document.createElement("div");
    summary.className = "compare-summary";
    if (data.discrepancies.length) {
        summary.innerHTML = `<div class="compare-disc-head">${data.discrepancies.length} discrepanc${data.discrepancies.length !== 1 ? "ies" : "y"} found</div>` +
            data.discrepancies.map(d => `<div class="actual-warn">${escapeHtml(d)}</div>`).join("");
    } else {
        summary.innerHTML = `<div class="actual-good">No discrepancies found. Great adherence!</div>`;
    }
    if (data.informational && data.informational.length) {
        summary.innerHTML += `<div class="compare-disc-head" style="margin-top:0.75rem;">Outside any plan (informational)</div>` +
            data.informational.map(a => `<div class="actual-muted">${a.date}: [${a.activity_type}] ${escapeHtml(a.activity_name)}</div>`).join("");
    }
    target.appendChild(summary);
}

// --- LEARNINGS (full manager) ---

async function fetchLearningsFull() {
    const sport = document.getElementById("lfilter-sport").value.trim();
    const confidence = document.getElementById("lfilter-confidence").value;
    const dormant = document.getElementById("lfilter-dormant").checked;
    const params = new URLSearchParams();
    if (sport) params.set("sport", sport);
    if (confidence) params.set("confidence", confidence);
    if (dormant) params.set("dormant", "1");

    const container = document.getElementById("learnings-list-container");
    try {
        const res = await fetch(`${API_BASE}/api/learnings?${params}`);
        const data = await res.json();
        const summary = data.summary || {};
        document.getElementById("learnings-summary-badge").innerText =
            `${summary.active || 0} active · ${summary.dormant || 0} dormant · ${summary.pending_demotion || 0} pending`;

        container.innerHTML = "";
        const learnings = data.learnings || [];
        if (!learnings.length) {
            container.innerHTML = `<div class="item-meta" style="padding:1rem;">No learnings match. Run 'data bootstrap' (CLI) to seed observations.</div>`;
            return;
        }
        learnings.forEach(l => container.appendChild(renderLearningRow(l)));
    } catch (e) {
        container.innerHTML = `<div class="log-line error">Failed to load learnings: ${escapeHtml(e.message)}</div>`;
    }
}

function renderLearningRow(l) {
    const sports = l.sports || "general";
    const conf = l.confidence || "tentative";
    const row = document.createElement("div");
    row.className = "learning-item" + (l.dormant ? " dormant" : "");

    let proposed = "";
    if (l.proposed_confidence) {
        const tgt = l.proposed_confidence === "retire" ? "retire" : l.proposed_confidence;
        proposed = `<div class="learning-proposed">⚠ proposed demotion → ${tgt}
            <button class="btn-link" onclick="learningAction(${l.id}, 'demote')">accept</button>
            <button class="btn-link" onclick="learningAction(${l.id}, 'keep')">keep</button></div>`;
    }
    row.innerHTML = `
        <div class="learning-main">
            <span class="learning-tag">[${l.id} · ${escapeHtml(sports)} · ${conf}]</span>
            ${escapeHtml(l.text)}
            ${l.dormant ? `<span class="learning-dormant">(dormant)</span>` : ""}
        </div>
        <div class="learning-actions"></div>
        ${proposed}
        <div class="learning-evidence" id="evidence-${l.id}" style="display:none;"></div>`;

    const actionsEl = row.querySelector(".learning-actions");
    const mkBtn = (label, cls, handler) => {
        const b = document.createElement("button");
        b.className = "btn-link" + (cls ? " " + cls : "");
        b.innerText = label;
        b.addEventListener("click", handler);
        return b;
    };
    actionsEl.appendChild(mkBtn("evidence", "", () => toggleEvidence(l.id)));
    actionsEl.appendChild(mkBtn("edit", "", () => editLearning(l.id, l.text)));
    actionsEl.appendChild(mkBtn("delete", "danger", () => learningAction(l.id, "delete")));
    return row;
}

window.toggleEvidence = async function(id) {
    const box = document.getElementById(`evidence-${id}`);
    if (!box) return;
    if (box.style.display !== "none") { box.style.display = "none"; return; }
    box.style.display = "block";
    box.innerHTML = `<span class="item-meta">Loading evidence...</span>`;
    try {
        const res = await fetch(`${API_BASE}/api/learnings/${id}/evidence`);
        const data = await res.json();
        if (!res.ok) { box.innerHTML = `<span class="log-line error">${escapeHtml(data.error)}</span>`; return; }
        const fmt = (arr) => arr.length ? arr.map(e => e.week_commencing).join(", ") : "—";
        box.innerHTML =
            `<div class="evidence-line"><span class="evidence-pos">Supporting weeks:</span> ${fmt(data.supporting)}</div>` +
            `<div class="evidence-line"><span class="evidence-neg">Contradicting weeks:</span> ${fmt(data.contradicting)}</div>`;
    } catch (e) { box.innerHTML = `<span class="log-line error">${escapeHtml(e.message)}</span>`; }
};

window.editLearning = function(id, text) {
    openModal("Edit Learning", [
        { id: "text", label: "Observation", type: "textarea", value: text, required: true },
    ], async (vals) => {
        const res = await fetch(`${API_BASE}/api/learnings/${id}`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: vals.text }),
        });
        const data = await res.json();
        if (res.ok) { logConsole(data.message || "Learning updated."); closeModal(); fetchLearningsFull(); }
        else logConsole(`Edit failed: ${data.error}`, "error");
    });
};

window.learningAction = async function(id, action) {
    if (action === "delete" && !confirm("Remove this learning permanently?")) return;
    let url, method;
    if (action === "delete") { url = `/api/learnings/${id}`; method = "DELETE"; }
    else { url = `/api/learnings/${id}/${action}`; method = "POST"; }
    try {
        const res = await fetch(`${API_BASE}${url}`, { method });
        const data = await res.json();
        if (res.ok) {
            logConsole(data.message || `Learning ${id} ${action} done.`);
            fetchLearningsFull();
            fetchStatus();
        } else logConsole(`Learning ${action} failed: ${data.error}`, "error");
    } catch (e) { logConsole(`Learning ${action} error: ${e.message}`, "error"); }
};

// --- HISTORY (activities / metrics / context) ---

function buildTable(rows, columns, emptyMsg) {
    if (!rows || !rows.length) return `<div class="item-meta">${emptyMsg}</div>`;
    const head = columns.map(c => `<th>${c.label}</th>`).join("");
    const body = rows.map(r => "<tr>" + columns.map(c => {
        let v = c.get ? c.get(r) : r[c.key];
        if (v == null) v = "";
        return `<td>${escapeHtml(String(v))}</td>`;
    }).join("") + "</tr>").join("");
    return `<table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function fetchHistory() {
    const from = document.getElementById("hist-from").value;
    const until = document.getElementById("hist-until").value;
    const range = new URLSearchParams();
    if (from) range.set("start_date", from);
    if (until) range.set("end_date", until);

    // Activities
    try {
        const res = await fetch(`${API_BASE}/api/activities?${range}`);
        const acts = await res.json();
        document.getElementById("activities-table").innerHTML = buildTable(acts, [
            { label: "Date", key: "date" },
            { label: "Type", key: "activity_type" },
            { label: "Name", key: "activity_name" },
            { label: "Duration", get: a => `${(a.duration_sec / 60).toFixed(0)}min` },
            { label: "Dist (km)", get: a => a.distance_km != null ? a.distance_km.toFixed(1) : "" },
            { label: "Avg HR", key: "avg_hr" },
            { label: "TSS", get: a => a.tss != null ? Math.round(a.tss) : "" },
            { label: "RPE", key: "rpe" },
        ], "No activities in range.");
    } catch (e) { logConsole(`Activities load error: ${e.message}`, "error"); }

    // Metrics
    try {
        const res = await fetch(`${API_BASE}/api/metrics?${range}`);
        const metrics = await res.json();
        document.getElementById("metrics-table").innerHTML = buildTable(metrics, [
            { label: "Date", key: "date" },
            { label: "RHR", key: "rhr" },
            { label: "HRV", key: "hrv" },
            { label: "Sleep", key: "sleep_score" },
            { label: "Stress", key: "stress" },
            { label: "ATL:CTL", get: m => (m.ctl > 0 && m.atl != null) ? (m.atl / m.ctl).toFixed(2) : "" },
        ], "No metrics in range.");
    } catch (e) { logConsole(`Metrics load error: ${e.message}`, "error"); }

    // Daily context
    try {
        const res = await fetch(`${API_BASE}/api/daily-context?${range}`);
        const ctx = await res.json();
        document.getElementById("context-table").innerHTML = buildTable(ctx, [
            { label: "Date", key: "date" },
            { label: "Metric", key: "metric" },
            { label: "Value", key: "value" },
            { label: "Note", key: "text" },
        ], "No daily-context signals in range.");
    } catch (e) { logConsole(`Context load error: ${e.message}`, "error"); }
}

// --- PROGRESS TAB (DESIGN_progress_timeline.md §7.3) ---
// V1 is the picture, not an app: an <img> pointing at GET /api/timeline.png plus
// quick-range buttons that set ?weeks= and reload it. The empty / still-warming
// states are drawn inside the PNG by the server renderer, identical to the bot
// photo. A 503 (matplotlib missing) shows the endpoint's install hint verbatim.

let progressWeeks = "8";

async function fetchProgress() {
    const img = document.getElementById("progress-image");
    const errEl = document.getElementById("progress-image-error");
    const url = `${API_BASE}/api/timeline.png?weeks=${progressWeeks}`;
    try {
        const res = await fetch(url);
        if (!res.ok) {
            // 503 (matplotlib absent) or 400 (bad weeks) — show the body text.
            const text = await res.text();
            img.style.display = "none";
            errEl.style.display = "";
            errEl.textContent = text || `Progress chart unavailable (HTTP ${res.status}).`;
            return;
        }
        const blob = await res.blob();
        if (img.dataset.objectUrl) URL.revokeObjectURL(img.dataset.objectUrl);
        const objectUrl = URL.createObjectURL(blob);
        img.dataset.objectUrl = objectUrl;
        img.src = objectUrl;
        img.style.display = "";
        errEl.style.display = "none";
    } catch (e) {
        logConsole(`Progress load error: ${e.message}`, "error");
    }
}

document.querySelectorAll(".progress-range-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".progress-range-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        progressWeeks = btn.dataset.weeks;
        fetchProgress();
    });
});

// --- DASHBOARD ACTION BUTTONS ---

document.getElementById("btn-pull-metrics").addEventListener("click", async () => {
    try {
        const res = await fetch(`${API_BASE}/api/metrics/pull`, { method: "POST" });
        const data = await res.json();
        logConsole(data.error || "Garmin sync is CLI-only.", "warning");
        if (data.command) logConsole(`Run: ${data.command}`, "system");
        fetchStatus();
    } catch (e) { logConsole(`Pull metrics error: ${e.message}`, "error"); }
});

document.getElementById("btn-generate-plan").addEventListener("click", async () => {
    logConsole("Requesting Coach to generate periodized plan strategy (macro/meso)...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/plan`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Periodization plan generated! ${data.mesocycles_count} mesocycles established.`);
            if (data.strategy) logConsole(`Overall Strategy:\n${data.strategy}`, "system");
            fetchStatus();
        } else logConsole(`Plan generation failed: ${data.error}`, "error");
    } catch (e) { logConsole(`AI Planning Error: ${e.message}`, "error"); }
});

document.getElementById("btn-delete-plan").addEventListener("click", async () => {
    if (activeGoalId == null) { logConsole("No active goal to delete a plan for.", "error"); return; }
    if (!confirm("Delete the periodization plan for the active goal?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/plan/${activeGoalId}`, { method: "DELETE" });
        const data = await res.json();
        if (res.ok) { logConsole(data.message || "Plan deleted."); fetchStatus(); }
        else logConsole(`Delete plan failed: ${data.error}`, "error");
    } catch (e) { logConsole(`Delete plan error: ${e.message}`, "error"); }
});

// --- Plan versions & rollback (see DESIGN_plan_rollback.md) ---
async function loadPlanVersions() {
    const listEl = document.getElementById("plan-versions-list");
    if (!listEl) return;
    listEl.innerHTML = `<div class="item-meta">Loading versions…</div>`;
    try {
        const params = activeGoalId != null ? `?goal_id=${activeGoalId}` : "";
        const res = await fetch(`${API_BASE}/api/plan/versions${params}`);
        const data = await res.json();
        const versions = (data && data.versions) || [];
        if (versions.length <= 1) {
            listEl.innerHTML = `<div class="item-meta">No earlier versions yet. `
                + `Regenerating this plan keeps the previous version here so you can roll back.</div>`;
            return;
        }
        listEl.innerHTML = versions.map(v => {
            const active = v.status !== "superseded";
            const created = v.created_at
                ? new Date(v.created_at).toLocaleDateString("en-US",
                    { month: "short", day: "numeric", year: "numeric" })
                : "?";
            let excerpt = (v.strategy || "").replace(/\s+/g, " ").trim();
            if (excerpt.length > 90) excerpt = excerpt.slice(0, 89) + "…";
            const badge = active
                ? `<span class="badge badge-success">ACTIVE</span>`
                : `<span class="badge badge-info">superseded</span>`;
            const action = active
                ? `<span class="item-meta">current</span>`
                : `<button class="btn btn-secondary btn-sm" data-diff="${v.id}" `
                  + `title="Compare this version against the active plan">`
                  + `<i class="fa-solid fa-code-compare"></i> Compare</button> `
                  + `<button class="btn btn-secondary btn-sm" data-version="${v.id}">`
                  + `<i class="fa-solid fa-rotate-left"></i> Restore</button>`;
            return `<div class="plan-version-row">`
                + `<div class="plan-version-head">${badge} `
                + `<span class="item-meta">ID ${v.id} · generated ${escapeHtml(created)}</span>`
                + `<span class="plan-version-action">${action}</span></div>`
                + (excerpt ? `<div class="si-desc">${escapeHtml(excerpt)}</div>` : "")
                + `</div>`;
        }).join("");
        listEl.querySelectorAll("button[data-version]").forEach(btn => {
            btn.addEventListener("click", () => rollbackToVersion(btn.dataset.version));
        });
        listEl.querySelectorAll("button[data-diff]").forEach(btn => {
            btn.addEventListener("click", () => loadPlanDiff(btn.dataset.diff));
        });
        hidePlanDiff();
    } catch (e) {
        listEl.innerHTML = `<div class="item-meta">Failed to load versions: ${escapeHtml(e.message)}</div>`;
    }
}

// --- Plan version comparison (GET /api/plan/diff; same structure the CLI's
// `plan diff` renders as text — see trainmate/plan_diff.py) ---

function hidePlanDiff() {
    const panel = document.getElementById("plan-diff-panel");
    if (panel) { panel.style.display = "none"; panel.innerHTML = ""; }
}

function diffLine(marker, text, cls) {
    return `<div class="pd-line pd-${cls}">`
        + `<span class="pd-marker">${marker}</span>`
        + `<span class="pd-text">${escapeHtml(text)}</span></div>`;
}

/** Renders one prose comparison. A block the coach rewrote wholesale stays collapsed
 *  behind a disclosure — expanded, it is just both versions in full (the web equivalent
 *  of the CLI's --full). */
function renderProse(prose) {
    if (!prose || !prose.changed) return `<div class="pd-empty">unchanged</div>`;
    const lines = prose.blocks.map(b =>
        b.removed.map(s => diffLine("−", s, "removed")).join("")
        + b.added.map(s => diffLine("+", s, "added")).join("")
    ).join("");
    if (!prose.rewritten) return lines;
    return `<details class="pd-rewritten"><summary>`
        + `rewritten (${prose.old_count} sentences → ${prose.new_count}) — show sentences`
        + `</summary>${lines}</details>`;
}

function renderMesocycles(entries) {
    const changed = (entries || []).filter(e => e.change !== "unchanged");
    if (!changed.length) return `<div class="pd-empty">unchanged</div>`;
    return changed.map(e => {
        if (e.change === "added" || e.change === "removed") {
            const added = e.change === "added";
            return diffLine(added ? "+" : "−",
                `${e.name} (${e.dates.start} → ${e.dates.end})`, added ? "added" : "removed");
        }
        const header = e.renamed ? `${e.from_name}  →  ${e.name}` : e.name;
        let out = diffLine("~", header, "changed");
        if (e.dates) {
            out += `<div class="pd-detail">dates ${escapeHtml(e.dates.from.start)} → `
                + `${escapeHtml(e.dates.from.end)}  ⇒  ${escapeHtml(e.dates.to.start)} → `
                + `${escapeHtml(e.dates.to.end)}</div>`;
        }
        for (const f of e.fields) {
            out += `<div class="pd-detail">${escapeHtml(f.field)}: `
                + `${escapeHtml(f.from ?? "—")}  ⇒  ${escapeHtml(f.to ?? "—")}</div>`;
        }
        if (e.focus) out += `<div class="pd-detail pd-focus">focus:</div>` + renderProse(e.focus);
        return out;
    }).join("");
}

/** A plan predating a snapshot column has nothing recorded — never read that as the
 *  inputs having been deleted. */
function renderMissing(missing) {
    if (missing === "both") return `<div class="pd-empty">not recorded on either version</div>`;
    const side = missing === "old" ? "A" : "B";
    return `<div class="pd-empty">not recorded on ${side} — that plan predates the snapshot</div>`;
}

function renderRecords(diff) {
    if (diff.missing) return renderMissing(diff.missing);
    if (!diff.added.length && !diff.removed.length && !diff.changed.length) {
        return `<div class="pd-empty">unchanged</div>`;
    }
    return diff.removed.map(r => diffLine("−", `[ID ${r.id}] ${r.title || ""}`, "removed")).join("")
        + diff.added.map(r => diffLine("+", `[ID ${r.id}] ${r.title || ""}`, "added")).join("")
        + diff.changed.map(r => diffLine("~", `[ID ${r.id}] ${r.title}`, "changed")
            + r.fields.map(f => `<div class="pd-detail">${escapeHtml(f.field)}: `
                + `${escapeHtml(String(f.from))}  ⇒  ${escapeHtml(String(f.to))}</div>`).join("")
        ).join("");
}

function renderThresholds(diff) {
    if (diff.missing) return renderMissing(diff.missing);
    if (!diff.added.length && !diff.removed.length && !diff.changed.length) {
        return `<div class="pd-empty">unchanged</div>`;
    }
    return diff.removed.map(t => diffLine("−", `${t.key}: ${t.value}`, "removed")).join("")
        + diff.added.map(t => diffLine("+", `${t.key}: ${t.value}`, "added")).join("")
        + diff.changed.map(t => diffLine("~",
            `${t.key}: ${t.from}  ⇒  ${t.to}`
            + (t.pct != null ? ` (${t.pct >= 0 ? "+" : ""}${t.pct.toFixed(1)}%)` : ""),
            "changed")).join("");
}

function diffSection(title, body) {
    return `<div class="pd-section"><div class="si-heading">${title}</div>${body}</div>`;
}

async function loadPlanDiff(fromVersion) {
    const panel = document.getElementById("plan-diff-panel");
    if (!panel) return;
    panel.style.display = "block";
    panel.innerHTML = `<div class="item-meta">Comparing…</div>`;
    try {
        const params = new URLSearchParams({ from_version: fromVersion });
        if (activeGoalId != null) params.set("goal_id", activeGoalId);
        const res = await fetch(`${API_BASE}/api/plan/diff?${params}`);
        const data = await res.json();
        if (!res.ok) {
            panel.innerHTML = `<div class="item-meta">${escapeHtml(data.error || "Comparison failed.")}</div>`;
            return;
        }
        const d = data.diff;
        const when = v => v.created_at
            ? new Date(v.created_at).toLocaleDateString("en-US",
                { month: "short", day: "numeric", year: "numeric" })
            : "?";
        panel.innerHTML = `<div class="pd-head">`
            + `<span class="pd-title">Plan ID ${d.from.id} <span class="pd-arrow">→</span> `
            + `ID ${d.to.id}</span>`
            + `<span class="item-meta">${escapeHtml(when(d.from))} → ${escapeHtml(when(d.to))}</span>`
            + `<button class="btn btn-secondary btn-sm pd-close" id="btn-close-plan-diff">`
            + `<i class="fa-solid fa-xmark"></i></button></div>`
            + diffSection("Strategy", renderProse(d.strategy))
            + diffSection("Macrocycle feedback", renderProse(d.feedback))
            + diffSection("Mesocycles", renderMesocycles(d.mesocycles))
            + diffSection("Goals considered", renderRecords(d.goals))
            + diffSection("Constraints considered", renderRecords(d.constraints))
            + diffSection("Thresholds considered", renderThresholds(d.thresholds));
        document.getElementById("btn-close-plan-diff")
            .addEventListener("click", hidePlanDiff);
    } catch (e) {
        panel.innerHTML = `<div class="item-meta">Comparison error: ${escapeHtml(e.message)}</div>`;
    }
}

async function rollbackToVersion(versionId) {
    if (!confirm("Roll back to this plan version? This archives the current plan's "
        + "upcoming workouts and restores that version's on Google Calendar.")) return;
    logConsole(`Rolling back plan to version ${versionId}…`, "system");
    try {
        const res = await fetch(`${API_BASE}/api/plan/rollback`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ goal_id: activeGoalId, version: Number(versionId) }),
        });
        const data = await res.json();
        if (res.ok) {
            logConsole(data.message || "Rollback complete.");
            fetchStatus();
            fetchWorkouts();
            loadPlanVersions();
        } else {
            logConsole(`Rollback failed: ${data.error}`, "error");
        }
    } catch (e) { logConsole(`Rollback error: ${e.message}`, "error"); }
}

document.getElementById("plan-versions").addEventListener("toggle", (e) => {
    if (e.target.open) loadPlanVersions();
});

document.getElementById("btn-generate-workouts").addEventListener("click", async () => {
    logConsole("Requesting Coach to generate workouts (microcycles)...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/workouts/generate`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Workouts generated! ${data.workouts_count} workouts scheduled and pushed to Google Calendar.`);
            if (data.reasoning) logConsole(`Coach Reasoning:\n${data.reasoning}`, "system");
            fetchStatus();
            fetchWorkouts();
        } else logConsole(`Workout generation failed: ${data.error}`, "error");
    } catch (e) { logConsole(`AI Workout Generation Error: ${e.message}`, "error"); }
});

document.getElementById("btn-adapt").addEventListener("click", async () => {
    logConsole("Running daily Garmin metrics adaptation check...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/adapt`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) { logConsole(`Adaptation check failed: ${data.error}`, "error"); return; }
        logConsole(`Daily Check Complete: ${data.reason}`, "system");
        if (!data.change_needed || !data.workouts || data.workouts.length === 0) {
            logConsole("Workouts remain as scheduled.", "system");
            return;
        }
        data.workouts.forEach(w => logConsole(`Proposed: ${w.date} ${w.sport_type} — ${w.title}`, "warning"));
        if (!confirm(`Apply ${data.workouts.length} proposed adaptation(s) and sync to Calendar?`)) {
            logConsole("Adaptations discarded.", "system");
            return;
        }
        const ap = await fetch(`${API_BASE}/api/adapt/apply`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workouts: data.workouts, reason: data.reason }),
        });
        const apData = await ap.json();
        if (ap.ok) { logConsole(apData.message || "Adaptations applied."); fetchStatus(); fetchWorkouts(); }
        else logConsole(`Apply failed: ${apData.error}`, "error");
    } catch (e) { logConsole(`Adaptation check error: ${e.message}`, "error"); }
});

document.getElementById("btn-push-workouts").addEventListener("click", async () => {
    logConsole("Pushing planned training sessions to Google Calendar...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/workouts/push`, { method: "POST" });
        const data = await res.json();
        if (res.ok) { logConsole(`Pushed! ${data.synced_count} workouts written to Google Calendar.`); fetchWorkouts(); }
        else logConsole(`Calendar push failed: ${data.error}`, "error");
    } catch (e) { logConsole(`Calendar Push Error: ${e.message}`, "error"); }
});

// --- FORMS ---

document.getElementById("form-add-goal").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = document.getElementById("goal-title").value;
    const target_date = document.getElementById("goal-date").value;
    const selectedChips = document.querySelectorAll("#goal-sports-chips .sport-chip.selected");
    const sports = Array.from(selectedChips).map(c => c.dataset.value);
    if (sports.length === 0) { logConsole("Failed to add goal: select at least one sport.", "error"); alert("Please select at least one sport."); return; }
    const priority = document.getElementById("goal-priority").value;
    const description = document.getElementById("goal-desc").value;
    logConsole(`Adding goal: ${title}...`, "system");
    try {
        const res = await fetch(`${API_BASE}/api/objectives`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, target_date, sport_type: sports, priority, description }),
        });
        if (res.ok) {
            logConsole(`Goal '${title}' added!`);
            document.getElementById("form-add-goal").reset();
            selectedChips.forEach(c => c.classList.remove("selected"));
            fetchObjectives(); fetchStatus();
        } else { const data = await res.json(); logConsole(`Add goal failed: ${data.error}`, "error"); }
    } catch (err) { logConsole(`Error adding goal: ${err.message}`, "error"); }
});

document.getElementById("form-add-event").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = document.getElementById("event-title").value;
    const start_date = document.getElementById("event-start").value;
    const end_date = document.getElementById("event-end").value;
    const rest = document.getElementById("event-rest").checked ? 1 : 0;
    const description = document.getElementById("event-desc").value;
    logConsole(`Logging constraint: ${title}...`, "system");
    try {
        const res = await fetch(`${API_BASE}/api/constraints`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, start_date, end_date, rest, description }),
        });
        if (res.ok) { logConsole(`Logged constraint '${title}'!`); document.getElementById("form-add-event").reset(); fetchEvents(); }
        else { const data = await res.json(); logConsole(`Log event failed: ${data.error}`, "error"); }
    } catch (err) { logConsole(`Error logging event: ${err.message}`, "error"); }
});

document.getElementById("form-add-workout").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
        date: document.getElementById("wadd-date").value,
        sport_type: document.getElementById("wadd-sport").value,
        title: document.getElementById("wadd-title").value,
        description: document.getElementById("wadd-desc").value,
        duration_minutes: document.getElementById("wadd-duration").value,
        rpe: document.getElementById("wadd-rpe").value,
        tss: document.getElementById("wadd-tss").value,
        reason: document.getElementById("wadd-reason").value || null,
    };
    logConsole(`Adding manual workout: ${payload.title}...`, "system");
    try {
        const res = await fetch(`${API_BASE}/api/workouts`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok) {
            logConsole(data.message || "Workout scheduled.");
            document.getElementById("form-add-workout").reset();
            fetchWorkouts();
        } else logConsole(`Add workout failed: ${data.error}`, "error");
    } catch (err) { logConsole(`Error adding workout: ${err.message}`, "error"); }
});

document.getElementById("btn-save-macro-feedback").addEventListener("click", async () => {
    if (!activeMacrocycleId) { logConsole("No active macrocycle to save feedback for.", "error"); return; }
    const feedback = document.getElementById("macro-feedback-input").value;
    logConsole("Saving strategy feedback...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/macrocycles/${activeMacrocycleId}/feedback`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feedback }),
        });
        const data = await res.json();
        if (res.ok) { logConsole("Strategy feedback saved."); document.getElementById("macro-feedback-notice").style.display = "block"; }
        else logConsole(`Failed to save strategy feedback: ${data.error}`, "error");
    } catch (e) { logConsole(`Error saving strategy feedback: ${e.message}`, "error"); }
});

document.getElementById("btn-save-meso-feedback").addEventListener("click", async () => {
    if (!activeMesocycleId) { logConsole("No active mesocycle to save feedback for.", "error"); return; }
    const feedback = document.getElementById("meso-feedback-input").value;
    logConsole("Saving block feedback...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/mesocycles/${activeMesocycleId}/feedback`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feedback }),
        });
        const data = await res.json();
        if (res.ok) { logConsole("Block feedback saved."); document.getElementById("meso-feedback-notice").style.display = "block"; }
        else logConsole(`Failed to save block feedback: ${data.error}`, "error");
    } catch (e) { logConsole(`Error saving block feedback: ${e.message}`, "error"); }
});

document.getElementById("btn-clear-logs").addEventListener("click", () => {
    const consoleLogs = document.getElementById("console-logs");
    if (consoleLogs) consoleLogs.innerHTML = "";
});

// Filter/refresh buttons
document.getElementById("btn-workout-refresh").addEventListener("click", fetchWorkouts);
document.getElementById("btn-compare-run").addEventListener("click", runCompare);
document.getElementById("btn-learnings-refresh").addEventListener("click", fetchLearningsFull);
document.getElementById("btn-history-refresh").addEventListener("click", fetchHistory);

document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") closeModal();
});

// --- INITIALIZATION ---
document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("#goal-sports-chips .sport-chip").forEach(chip => {
        chip.addEventListener("click", () => chip.classList.toggle("selected"));
    });
    // Default workouts filter to today.
    const wf = document.getElementById("wfilter-from");
    if (wf) wf.value = todayStr();

    fetchStatus();
    fetchObjectives();
    fetchEvents();
});
