// TrainMate dashboard — a READ-ONLY view over the database (ARCHITECTURE.md §8).
// Nothing here writes: the API refuses every mutating verb, so each panel that used to
// carry a button now names the CLI command that does the job instead.
const API_BASE = "";
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
    const parts = String(dateStr).split("-");
    return new Date(parts[0], parts[1] - 1, parts[2]);
}

function todayStr() {
    const d = new Date();
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function sportLabel(sport) {
    return (sport || "").replace(/_/g, " ").toUpperCase();
}

function sportTitle(sport) {
    return (sport || "").replace(/_/g, " ");
}

const SPORT_ICONS = {
    running: "fa-person-running", cycling: "fa-bicycle", hiking: "fa-mountain-sun",
    strength_training: "fa-dumbbell", yoga: "fa-spa", ski_touring: "fa-person-skiing-nordic",
    rowing: "fa-ship", downhill_skiing: "fa-person-skiing", resort_skiing: "fa-person-skiing",
    swimming: "fa-person-swimming", rest: "fa-bed",
};

/** A zone cell's time, capped to four characters exactly as `progress.fmt_zone_cell`
 *  renders it in the terminal — `55m`, `5h00`, `12h`, `—` for none. */
function fmtZoneCell(seconds) {
    const minutes = Math.round((seconds || 0) / 60);
    if (minutes <= 0) return "—";
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    return hours >= 10 ? `${hours}h` : `${hours}h${String(minutes % 60).padStart(2, "0")}`;
}

function fmtDuration(seconds) {
    const minutes = Math.round((seconds || 0) / 60);
    if (minutes < 60) return `${minutes}min`;
    return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}`;
}

/** Fills a <select> with the sports actually present in the data, so the filter can
 *  never fall behind the canonical sport list the way a hardcoded <option> set did. */
function populateSportFilter(selectEl, sports) {
    if (!selectEl) return;
    const current = selectEl.value;
    const opts = [`<option value="">All</option>`].concat(
        sports.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(sportTitle(s))}</option>`)
    );
    selectEl.innerHTML = opts.join("");
    if (sports.includes(current)) selectEl.value = current;
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
        else if (viewId === "view-benchmarks") fetchBenchmarks();
        else if (viewId === "view-progress") { fetchProgress(); fetchZones(); }
    }
};

window.switchTab = function(tabId) {
    document.querySelectorAll(".tab-content").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".accordion-card .tab-btn").forEach(el => el.classList.remove("active"));

    document.getElementById(tabId).classList.add("active");
    const btn = Array.from(document.querySelectorAll(".accordion-card .tab-btn")).find(
        b => (tabId === "tab-goals" && b.innerText.includes("Goals")) ||
             (tabId === "tab-events" && b.innerText.includes("Constraints"))
    );
    if (btn) btn.classList.add("active");
};

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
            goalCountdownEl.innerText = "Add a goal with 'tm goal add'";
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
            strategyCard.style.display = "block";
            if (noStrategyCard) noStrategyCard.style.display = "none";
            document.getElementById("strategy-philosophy").innerText =
                data.macrocycle.strategy;
            renderStrategyInputs(data.macrocycle);
            renderPlanFeedback(data.plan_feedback);
            renderTimeline(data.mesocycles, data.plan_feedback);

            if (data.macrocycle.created_at) {
                const created = new Date(data.macrocycle.created_at);
                const formattedDate = created.toLocaleDateString("en-US", {
                    month: "short", day: "numeric", year: "numeric"
                });
                const formattedTime = created.toLocaleTimeString("en-US", {
                    hour: "numeric", minute: "2-digit"
                });
                document.getElementById("strategy-generated-at").innerText =
                    `Generated ${formattedDate} at ${formattedTime}`;
            } else {
                document.getElementById("strategy-generated-at").innerText = "";
            }
        } else {
            strategyCard.style.display = "none";
            if (noStrategyCard) noStrategyCard.style.display = "block";
        }

        const banner = document.getElementById("config-warning-banner");
        if (banner) {
            if (data.config_mismatch) {
                banner.style.display = "flex";
                logConsole(
                    "Warning: config.yaml has changed since the active periodization "
                    + "plan was generated. Run 'tm plan generate' to update.", "warning"
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
        el.innerText = "No observations cached yet. Run 'tm data bootstrap' (CLI) to "
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
        el.innerText = "Garmin data: never pulled — run 'tm data pull' (CLI).";
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

/** One note of the plan's feedback log: date, what it was filed to, the text. */
function feedbackNote(n) {
    const date = n.created_at ? String(n.created_at).slice(0, 10) : "";
    return `<div class="pf-note">`
        + `<span class="pf-meta">${escapeHtml(date)}`
        + ` · ${escapeHtml(n.mesocycle_name || "plan-level")}</span>`
        + `<span class="si-desc">${escapeHtml(n.text || "")}</span></div>`;
}

/** The notes the athlete addressed to the next plan version. Read-only here: they are
 *  written with `tm plan feedback` and are consumed by the next generation
 *  (DESIGN_plan_feedback.md §8). */
function renderPlanFeedback(notes) {
    const el = document.getElementById("strategy-feedback");
    if (!el) return;
    const all = notes || [];
    if (!all.length) { el.innerHTML = ""; return; }
    el.innerHTML = `<div class="si-heading"><i class="fa-solid fa-comments"></i> `
        + `Your feedback on this plan (${all.length} pending)</div>`
        + all.map(feedbackNote).join("");
}

// Renders the goals and constraints the plan was generated from. These are snapshotted
// on the macrocycle (server-side), so they reflect the inputs the plan was built on
// rather than the current live records, which may since have changed.
function renderStrategyInputs(macrocycle) {
    const el = document.getElementById("strategy-inputs");
    if (!el) return;

    const rawGoals = macrocycle.goals_snapshot;
    // New snapshots carry constraint fields; legacy plans (pre-constraints-rename)
    // carry the old lifeevents_snapshot — read whichever is present.
    const rawEvents = macrocycle.constraints_snapshot ?? macrocycle.lifeevents_snapshot;
    // Every active constraint at generation time (not just the replan=1 subset `rawEvents`
    // fingerprints), tagged with `replan`. Null on plans predating this column.
    const rawAllEvents = macrocycle.all_constraints_snapshot;
    if (rawGoals == null && rawEvents == null) {
        el.innerHTML = `<div class="strategy-inputs-note">`
            + `Inputs considered: not recorded (plan predates input snapshots).</div>`;
        return;
    }

    let goals = [], events = [], allEvents = null;
    try { goals = rawGoals ? JSON.parse(rawGoals) : []; } catch (e) { goals = []; }
    try { events = rawEvents ? JSON.parse(rawEvents) : []; } catch (e) { events = []; }
    try { allEvents = rawAllEvents ? JSON.parse(rawAllEvents) : null; } catch (e) { allEvents = null; }
    // The tactical (replan=0) constraints the prompt saw but the fingerprint didn't —
    // shown separately so "Constraints considered" never reads as "None" while one of
    // these plainly shaped the strategy text.
    const tactical = allEvents ? allEvents.filter(e => !e.replan) : null;

    const goalItems = goals.length
        ? goals.map(g => `<li>`
            + `<span class="si-title">${escapeHtml(g.title || "")}</span> `
            + `<span class="si-meta">(${escapeHtml((g.sport_type || "").toUpperCase())}) `
            + `· ${escapeHtml(g.target_date || "")}</span>`
            + (g.description ? `<div class="si-desc">${escapeHtml(g.description)}</div>` : "")
            + `</li>`).join("")
        : `<li class="si-empty">None</li>`;

    const constraintItems = (list) => list.length
        ? list.map(e => {
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

    const tacticalSection = tactical ? `
            <div class="si-section">
                <div class="si-heading"><i class="fa-solid fa-calendar-check"></i> Also active (tactical — did not trigger replan)</div>
                <ul class="si-list">${constraintItems(tactical)}</ul>
            </div>` : "";

    el.innerHTML = `
        <details class="strategy-inputs-details">
            <summary>Inputs considered (${goals.length} goal${goals.length === 1 ? "" : "s"}, `
                + `${events.length} constraint${events.length === 1 ? "" : "s"})</summary>
            <div class="si-section">
                <div class="si-heading"><i class="fa-solid fa-flag-checkered"></i> Goals considered</div>
                <ul class="si-list">${goalItems}</ul>
            </div>
            <div class="si-section">
                <div class="si-heading"><i class="fa-solid fa-calendar-day"></i> Constraints considered (plan-shaping)</div>
                <ul class="si-list">${constraintItems(events)}</ul>
            </div>${tacticalSection}
        </details>`;
}

function renderTimeline(mesocycles, planFeedback) {
    const container = document.getElementById("web-timeline-container");
    if (!container) return;
    container.innerHTML = "";

    const detailsBox = document.getElementById("cycle-details-box");
    const detailsName = document.getElementById("cycle-details-name");
    const detailsDates = document.getElementById("cycle-details-dates");
    const detailsFocus = document.getElementById("cycle-details-focus");
    const detailsFeedback = document.getElementById("cycle-details-feedback");

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
            const filed = (planFeedback || []).filter(n => n.mesocycle_id === m.id);
            detailsFeedback.innerHTML = filed.length
                ? `<div class="si-heading"><i class="fa-solid fa-comment-medical"></i> Block feedback</div>`
                  + filed.map(feedbackNote).join("")
                : "";
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
    // deload, or intensity block doing its job (training_load.md §3/§4).
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

// --- OBJECTIVES & CONSTRAINTS (read-only listings) ---

async function fetchObjectives() {
    try {
        const res = await fetch(`${API_BASE}/api/objectives`);
        if (!res.ok) throw new Error();
        const goals = await res.json();
        const container = document.getElementById("goals-list");
        container.innerHTML = "";
        if (goals.length === 0) {
            container.innerHTML = `<div class="item-meta" style="padding:0.5rem;">No goals yet.</div>`;
            return;
        }
        goals.forEach(g => {
            const sportsList = g.sport_type.split(',').map(s => sportTitle(s.trim())).join(', ');
            const item = document.createElement("div");
            item.className = "list-item";
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-title">${escapeHtml(g.title)} (${escapeHtml(sportsList)})</span>
                    <span class="item-meta">ID ${g.id} · Target: ${escapeHtml(g.target_date)} · ${escapeHtml(g.status)}</span>
                    ${g.description ? `<span class="si-desc">${escapeHtml(g.description)}</span>` : ""}
                </div>`;
            container.appendChild(item);
        });
    } catch (e) {
        logConsole("Failed to load goals", "error");
    }
}

// A constraint is advisory prose the coach works around; the one toggle is `rest`, a
// deterministic full no-training window (DESIGN_constraints.md rev 6).
async function fetchEvents() {
    try {
        const res = await fetch(`${API_BASE}/api/constraints`);
        if (!res.ok) throw new Error();
        const events = await res.json();
        const container = document.getElementById("events-list");
        container.innerHTML = "";
        if (events.length === 0) {
            container.innerHTML = `<div class="item-meta" style="padding:0.5rem;">No active or upcoming constraints.</div>`;
            return;
        }
        events.forEach(ev => {
            const badge = ev.rest
                ? `<span class="badge badge-danger">no training</span>`
                : `<span class="badge badge-info">advisory</span>`;
            const item = document.createElement("div");
            item.className = "list-item";
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-title">${escapeHtml(ev.title)} ${badge}</span>
                    <span class="item-meta">ID ${ev.id} · ${escapeHtml(ev.start_date)} → ${escapeHtml(ev.end_date)}`
                    + (ev.replan ? " · replan" : "") + `</span>
                    ${ev.description ? `<span class="si-desc">${escapeHtml(ev.description)}</span>` : ""}
                </div>`;
            container.appendChild(item);
        });
    } catch (e) {
        logConsole("Failed to load constraints", "error");
    }
}

// --- COACHING MODEL (`model list`) ---

async function fetchModels() {
    const el = document.getElementById("models-list");
    if (!el) return;
    try {
        const res = await fetch(`${API_BASE}/api/models`);
        const data = await res.json();
        const rows = data.models || [];
        if (!rows.length) { el.innerHTML = `<div class="item-meta">No models configured.</div>`; return; }
        const source = data.source ? ` <span class="item-meta">(${escapeHtml(data.source)})</span>` : "";
        el.innerHTML = `<div class="model-active">Active: <code>${escapeHtml(data.active || "?")}</code>${source}</div>`
            + rows.map(m => `<div class="model-row${m.active ? " is-active" : ""}">`
                + `<span class="model-num">${m.number == null ? "·" : m.number}</span>`
                + `<code>${escapeHtml(m.model)}</code>`
                + (m.active ? ` <span class="badge badge-success">active</span>` : "")
                + `</div>`).join("");
    } catch (e) {
        el.innerHTML = `<div class="item-meta">Failed to load models: ${escapeHtml(e.message)}</div>`;
    }
}

// --- WORKOUTS (read-only listing) ---

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
            container.innerHTML = `<div class="item-meta" style="text-align:center; padding: 2rem;">`
                + `No workouts in range. Generate some with 'tm workout generate'.</div>`;
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

    // Derived facts come from the API (calendar_status / modification_markers),
    // mirroring the CLI markers (ARCHITECTURE.md §5).
    const modMarkers = w.modification_markers || [];
    const calStatus = w.calendar_status || "unpushed";
    const isRemoved = !!w.removed;
    const isManual = w.source === "manual";

    let cardClass = "workout-card";
    if (isRemoved) cardClass += " removed";
    else if (modMarkers.length) cardClass += " adapted";
    else if (calStatus === "synced") cardClass += " synced";

    const iconGlyph = SPORT_ICONS[w.sport_type] || "fa-dumbbell";
    const iconClass = `workout-sport-icon ${w.sport_type || "rest"}`;

    const badges = [];
    // One badge per marker: a session eased twice and then swapped shows both
    // (DESIGN_workout_revisions.md §12).
    for (const marker of modMarkers) {
        const cls = marker.startsWith("ADAPTED") ? "adapted"
            : marker === "SWAPPED" ? "swapped"
            : marker === "REPLACED" ? "replaced" : "adapted";
        badges.push(`<span class="wbadge ${cls}">${escapeHtml(marker)}</span>`);
    }
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
        <div class="${iconClass}"><i class="fa-solid ${iconGlyph}"></i></div>`;
    return item;
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
            } else if (r.pending) {
                html += `<div class="compare-line"><span class="compare-label">ACTUAL</span> <span class="actual-muted">(not yet — still ahead today)</span></div>`;
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
            // Each entry carries its facts plus a rendered `text`; the kind is exposed
            // as a data attribute so styling never has to read the sentence.
            data.discrepancies.map(d =>
                `<div class="actual-warn" data-kind="${escapeHtml(d.kind)}">${escapeHtml(d.text)}</div>`
            ).join("");
    } else {
        summary.innerHTML = `<div class="actual-good">No discrepancies found. Great adherence!</div>`;
    }
    if (data.informational && data.informational.length) {
        summary.innerHTML += `<div class="compare-disc-head" style="margin-top:0.75rem;">Outside any plan (informational)</div>` +
            data.informational.map(a => `<div class="actual-muted">${a.date}: [${a.activity_type}] ${escapeHtml(a.activity_name)}</div>`).join("");
    }
    target.appendChild(summary);
}

// --- TIME IN ZONE (DESIGN_intensity_distribution.md §9.6/§9.8) ---
// The web form of `tm progress -z`: one table per qualifying sport, measured behind
// today and prescribed ahead of it. Every judgement (which sports, which currency, what
// counts as undercounted) is made server-side by the same functions the CLI calls — this
// only draws the result.

let zonesWeeks = "8";

async function fetchZones() {
    const container = document.getElementById("zones-container");
    if (!container) return;
    container.innerHTML = `<div class="item-meta">Loading zone distribution…</div>`;
    const params = new URLSearchParams({ weeks: zonesWeeks });
    const currency = document.getElementById("zfilter-currency").value;
    if (currency) params.set("currency", currency);
    try {
        const res = await fetch(`${API_BASE}/api/zones?${params}`);
        const data = await res.json();
        if (!res.ok) {
            container.innerHTML = `<div class="item-meta">${escapeHtml(data.error || "Zones unavailable.")}</div>`;
            return;
        }
        renderZones(data);
    } catch (e) {
        container.innerHTML = `<div class="item-meta">Zone load error: ${escapeHtml(e.message)}</div>`;
    }
}

function renderZones(data) {
    const container = document.getElementById("zones-container");
    const badge = document.getElementById("zones-window-badge");
    if (badge) {
        const w = data.window || {};
        badge.innerText = `${w.weeks === "all" ? "all weeks" : `${w.weeks} weeks`} `
            + `· ${fmtDuration(w.window_seconds || 0)} total`;
    }

    const parts = [];
    const sports = data.sports || [];
    if (!sports.length) {
        parts.push(`<div class="item-meta">No sport has recorded zone data in this window. `
            + `Zones come from HR or power streams on synced activities.</div>`);
    }

    sports.forEach(s => {
        const nZones = s.zone_labels.length;
        const header = `<div class="zone-head">`
            + `<span class="zone-sport">${escapeHtml(sportTitle(s.sport))}</span>`
            + `<span class="badge badge-info">${escapeHtml(s.tag)} ${Math.round((s.coverage || 0) * 100)}%</span>`
            + `<span class="item-meta">${fmtDuration(s.sport_seconds)} in window</span>`
            + `</div>`;

        const cols = s.zone_labels.map((_, i) => `<th>Z${i + 1}</th>`).join("");
        const rows = s.weeks.map(wk => {
            const cells = [];
            const total = (wk.seconds || []).reduce((a, b) => a + b, 0);
            for (let i = 0; i < nZones; i++) {
                const secs = wk.seconds ? wk.seconds[i] : 0;
                cells.push(`<td>${escapeHtml(fmtZoneCell(secs))}</td>`);
            }
            // The stacked bar is the web's addition over the terminal: the same numbers,
            // read as a shape. Weeks with nothing recorded draw no bar at all.
            const bar = total > 0
                ? `<div class="zone-bar">` + (wk.seconds || []).map((secs, i) =>
                    secs > 0
                        ? `<span class="zone-seg z${i + 1}" style="width:${(secs / total) * 100}%" `
                          + `title="Z${i + 1} ${escapeHtml(s.zone_labels[i])}: ${escapeHtml(fmtZoneCell(secs))}"></span>`
                        : ""
                  ).join("") + `</div>`
                : `<div class="zone-bar empty"></div>`;

            const marks = [];
            if (wk.undercounted) marks.push(`<span class="zone-mark" title="recording covered less of this week than the sport's bar — the row understates it">!</span>`);
            if (wk.in_progress) marks.push(`<span class="zone-mark" title="week in progress">*</span>`);
            if (wk.currency_mismatch) marks.push(`<span class="zone-mark" title="planned in the other currency; not converted">≠</span>`);

            const cls = ["zone-row"];
            if (wk.future) cls.push("future");
            if (wk.in_progress) cls.push("current");
            return `<tr class="${cls.join(" ")}">`
                + `<td class="zone-week">${escapeHtml(wk.week_commencing)}${marks.join("")}</td>`
                + cells.join("")
                + `<td class="zone-bar-cell">${bar}</td></tr>`;
        }).join("");

        parts.push(`<div class="zone-table-wrap">${header}`
            + `<table class="data-table zone-table"><thead><tr><th>Week</th>${cols}<th></th></tr></thead>`
            + `<tbody>${rows}</tbody></table></div>`);
    });

    const om = data.omitted || {};
    const notes = [];
    if ((om.low_volume || []).length) {
        notes.push(`${om.low_volume.map(sportTitle).join(", ")} omitted `
            + `(under ${Math.round((om.min_share || 0.1) * 100)}% of the window's duration)`);
    }
    if ((om.no_zone_data || []).length) {
        notes.push(`${om.no_zone_data.map(sportTitle).join(", ")} omitted (no zone data)`);
    }
    if ((data.window || {}).hidden_weeks) {
        notes.push(`${data.window.hidden_weeks} more week(s) outside this window`);
    }
    notes.push(`weeks past today show what the plan prescribes`);
    parts.push(`<div class="item-meta zone-legend">${escapeHtml(notes.join(" · "))}</div>`);

    container.innerHTML = parts.join("");
}

// --- BENCHMARKS (DESIGN_benchmark_workouts.md) ---

async function fetchBenchmarks() {
    const listEl = document.getElementById("benchmarks-list");
    const stripEl = document.getElementById("thresholds-strip");
    const params = new URLSearchParams();
    const sport = document.getElementById("bfilter-sport").value.trim();
    const kind = document.getElementById("bfilter-kind").value.trim();
    if (sport) params.set("sport", sport);
    if (kind) params.set("kind", kind);

    try {
        const res = await fetch(`${API_BASE}/api/benchmarks?${params}`);
        const data = await res.json();
        if (!res.ok) {
            listEl.innerHTML = `<div class="item-meta">${escapeHtml(data.error || "Failed to load.")}</div>`;
            return;
        }

        const thresholds = data.thresholds || [];
        stripEl.innerHTML = thresholds.length
            ? `<div class="threshold-strip">` + thresholds.map(t =>
                // `formatted` already carries the unit (`benchmarks.format_value`), so the
                // box prints it alone rather than re-deriving or repeating the suffix.
                `<div class="threshold-box">`
                + `<div class="threshold-kind">${escapeHtml(t.label)}</div>`
                + `<div class="threshold-value">${escapeHtml(t.formatted)}</div></div>`).join("") + `</div>`
            : `<div class="item-meta">No thresholds on record. `
              + `Record a test with 'tm benchmark record'.</div>`;

        const rows = data.results || [];
        document.getElementById("benchmarks-count").innerText =
            `${rows.length} result${rows.length === 1 ? "" : "s"}`;
        if (!rows.length) {
            listEl.innerHTML = `<div class="item-meta">No benchmark results recorded yet.</div>`;
            return;
        }
        listEl.innerHTML = rows.map(r => {
            // `delta` already carries the direction-aware sign (a faster pace reads
            // positive), so the colour follows `improvement`, never the raw arithmetic.
            const delta = r.delta
                ? `<span class="bm-delta ${r.improvement ? "up" : "down"}">${escapeHtml(r.delta)}</span>`
                : `<span class="item-meta">first</span>`;
            return `<div class="bm-row">`
                + `<div class="bm-main">`
                + `<span class="bm-kind">${escapeHtml(r.label)}</span> `
                + `<span class="bm-value">${escapeHtml(r.formatted)}</span> ${delta}`
                + `</div>`
                + `<div class="item-meta">ID ${r.id} · ${escapeHtml(r.date)} · `
                + `${escapeHtml(sportTitle(r.sport_type))} · ${escapeHtml(r.source || "test")}</div>`
                + (r.note ? `<div class="si-desc">${escapeHtml(r.note)}</div>` : "")
                + `</div>`;
        }).join("");
    } catch (e) {
        listEl.innerHTML = `<div class="item-meta">Benchmark load error: ${escapeHtml(e.message)}</div>`;
    }
}

// --- LEARNINGS (read-only, with evidence) ---

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
            container.innerHTML = `<div class="item-meta" style="padding:1rem;">No learnings match. `
                + `Run 'tm data bootstrap' (CLI) to seed observations.</div>`;
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
        proposed = `<div class="learning-proposed">⚠ proposed demotion → ${escapeHtml(tgt)} `
            + `<span class="item-meta">— resolve with 'tm learnings demote ${l.id}' or `
            + `'tm learnings keep ${l.id}'</span></div>`;
    }
    row.innerHTML = `
        <div class="learning-main">
            <span class="learning-tag">[${l.id} · ${escapeHtml(sports)} · ${escapeHtml(conf)}]</span>
            ${escapeHtml(l.text)}
            ${l.dormant ? `<span class="learning-dormant">(dormant)</span>` : ""}
        </div>
        <div class="learning-actions"></div>
        ${proposed}
        <div class="learning-evidence" id="evidence-${l.id}" style="display:none;"></div>`;

    const actionsEl = row.querySelector(".learning-actions");
    const b = document.createElement("button");
    b.className = "btn-link";
    b.innerText = "evidence";
    b.addEventListener("click", () => toggleEvidence(l.id));
    actionsEl.appendChild(b);
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

    // Daily context — vocabulary, calendar strips, then the raw rows.
    try {
        const [ctxRes, vocabRes] = await Promise.all([
            fetch(`${API_BASE}/api/daily-context?${range}`),
            fetch(`${API_BASE}/api/daily-context/metrics`),
        ]);
        const ctx = await ctxRes.json();
        const vocab = await vocabRes.json();
        renderContextVocab(vocab.metrics || []);
        renderContextChart(ctx);
        document.getElementById("context-table").innerHTML = buildTable(ctx, [
            { label: "Date", key: "date" },
            { label: "Metric", key: "metric" },
            { label: "Value", key: "value" },
            { label: "Note", key: "text" },
        ], "No daily-context signals in range.");
    } catch (e) { logConsole(`Context load error: ${e.message}`, "error"); }
}

/** `context list-metrics`: which signals exist at all, how many rows each has and the
 *  span it covers — the vocabulary behind the strips below. */
function renderContextVocab(metrics) {
    const el = document.getElementById("context-vocab");
    if (!el) return;
    if (!metrics.length) {
        el.innerHTML = `<div class="item-meta">No context signals recorded. `
            + `Add one with 'tm context add'.</div>`;
        return;
    }
    el.innerHTML = metrics.map(m =>
        `<span class="ctx-chip" title="${escapeHtml(m.first_date)} → ${escapeHtml(m.last_date)}">`
        + `${escapeHtml(m.metric)} <span class="ctx-chip-count">${m.count}</span></span>`).join("");
}

/** One row per metric, one cell per day in the loaded range: a calendar strip whose
 *  shading is the value's rank within that metric (metrics have no shared scale — sleep
 *  hours and units of alcohol cannot share a ramp). Days with no signal stay blank. */
function renderContextChart(rows) {
    const el = document.getElementById("context-chart");
    const badge = document.getElementById("context-window-badge");
    if (!el) return;
    if (!rows || !rows.length) {
        el.innerHTML = "";
        if (badge) badge.innerText = "";
        return;
    }

    const dates = rows.map(r => r.date).sort();
    const start = parseLocalDate(dates[0]);
    const end = parseLocalDate(dates[dates.length - 1]);
    const days = [];
    for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
        const pad = n => String(n).padStart(2, "0");
        days.push(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
    }
    if (badge) badge.innerText = `${dates[0]} → ${dates[dates.length - 1]} · ${rows.length} signals`;

    const byMetric = new Map();
    rows.forEach(r => {
        if (!byMetric.has(r.metric)) byMetric.set(r.metric, new Map());
        byMetric.get(r.metric).set(r.date, r);
    });

    const html = Array.from(byMetric.entries()).map(([metric, byDate]) => {
        const values = Array.from(byDate.values())
            .map(r => Number(r.value)).filter(v => Number.isFinite(v));
        const min = values.length ? Math.min(...values) : 0;
        const max = values.length ? Math.max(...values) : 0;
        const cells = days.map(day => {
            const row = byDate.get(day);
            if (!row) return `<span class="ctx-cell" title="${escapeHtml(day)}: —"></span>`;
            const v = Number(row.value);
            // A metric whose values never vary still deserves a visible mark, so a flat
            // series pins to full intensity rather than dividing by a zero span.
            const level = Number.isFinite(v) && max > min
                ? Math.round(((v - min) / (max - min)) * 4) + 1
                : 5;
            const label = [day, row.value != null ? `value ${row.value}` : null, row.text]
                .filter(Boolean).join(" · ");
            return `<span class="ctx-cell lvl${level}" title="${escapeHtml(label)}"></span>`;
        }).join("");
        return `<div class="ctx-row"><span class="ctx-label">${escapeHtml(metric)}</span>`
            + `<span class="ctx-strip">${cells}</span></div>`;
    }).join("");

    el.innerHTML = html
        + `<div class="item-meta ctx-axis">${escapeHtml(days[0])} → ${escapeHtml(days[days.length - 1])}`
        + ` · shading is each signal's own range</div>`;
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
        zonesWeeks = btn.dataset.weeks;
        fetchProgress();
        fetchZones();
    });
});

// --- Plan versions & comparison (see DESIGN_plan_rollback.md; rolling back is
//     `tm plan rollback`, a CLI action) ---

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
                + `Regenerating this plan keeps the previous version here so you can compare it.</div>`;
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
                  + `<i class="fa-solid fa-code-compare"></i> Compare</button>`;
            return `<div class="plan-version-row">`
                + `<div class="plan-version-head">${badge} `
                + `<span class="item-meta">ID ${v.id} · generated ${escapeHtml(created)}</span>`
                + `<span class="plan-version-action">${action}</span></div>`
                + (excerpt ? `<div class="si-desc">${escapeHtml(excerpt)}</div>` : "")
                + `</div>`;
        }).join("");
        listEl.querySelectorAll("button[data-diff]").forEach(btn => {
            btn.addEventListener("click", () => loadPlanDiff(btn.dataset.diff));
        });
        listEl.insertAdjacentHTML("beforeend",
            `<div class="cli-guidance"><i class="fa-solid fa-terminal"></i> `
            + `Restore one with <code>tm plan rollback --version &lt;id&gt;</code>.</div>`);
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

/** Each version's own feedback notes. An append-only log is not prose-diffed: for
 *  adjacent versions, A's notes are what drove B (DESIGN_plan_feedback.md §8). */
function renderFeedbackSides(entry) {
    if (!entry) return `<div class="pd-empty">none</div>`;
    return ["from", "to"].map((side, i) => {
        const notes = entry[side] || [];
        const body = notes.length
            ? notes.map(n => `<div class="pd-detail">${escapeHtml(n.date)} · `
                + `${escapeHtml(n.filing || "plan-level")} · ${escapeHtml(n.text)}</div>`).join("")
            : `<div class="pd-empty">none</div>`;
        return `<div class="pd-detail"><b>${i === 0 ? "A" : "B"}</b>:</div>${body}`;
    }).join("");
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
            + diffSection("Athlete feedback", renderFeedbackSides(d.feedback))
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

document.getElementById("plan-versions").addEventListener("toggle", (e) => {
    if (e.target.open) loadPlanVersions();
});

// --- Workout changes (see DESIGN_workout_revisions.md §10; undoing one is
//     `tm workout rollback`) ---

async function loadWorkoutBatches() {
    const listEl = document.getElementById("workout-batches-list");
    if (!listEl) return;
    listEl.innerHTML = `<div class="item-meta">Loading batches…</div>`;
    try {
        const res = await fetch(`${API_BASE}/api/workouts/batches`);
        const data = await res.json();
        const batches = (data && data.batches) || [];
        if (!batches.length) {
            listEl.innerHTML = `<div class="item-meta">Nothing has written workouts yet. `
                + `Every command that does is listed here, and every one is undoable.</div>`;
            return;
        }
        listEl.innerHTML = batches.map(b => {
            const when = new Date(b.created_at).toLocaleString("en-US",
                { month: "short", day: "numeric", year: "numeric",
                  hour: "2-digit", minute: "2-digit" });
            // A change that appended nothing — an adapt that held — is listed as itself.
            const state = b.held
                ? `<span class="item-meta">held</span>`
                : b.restorable
                    ? `<span class="item-meta">${b.restorable} upcoming</span>`
                    : `<span class="item-meta">all in the past</span>`;
            const plans = (b.macrocycle_ids || []).join(", ");
            const span = b.first_date
                ? `${escapeHtml(b.first_date)} → ${escapeHtml(b.last_date)}`
                : "nothing changed";
            return `<div class="plan-version-row">`
                + `<div class="plan-version-head">`
                + `<span class="badge badge-info">${escapeHtml(b.kind)}</span> `
                + `<span class="item-meta">${escapeHtml(when)}`
                + (plans ? ` · plan ID ${escapeHtml(plans)}` : "") + `</span>`
                + `<span class="plan-version-action">${state}</span></div>`
                + `<div class="si-desc">${span}</div>`
                + `</div>`;
        }).join("");
        listEl.insertAdjacentHTML("beforeend",
            `<div class="cli-guidance"><i class="fa-solid fa-terminal"></i> `
            + `Undo a change with <code>tm workout rollback</code>.</div>`);
    } catch (e) {
        listEl.innerHTML = `<div class="item-meta">Failed to load batches: ${escapeHtml(e.message)}</div>`;
    }
}

document.getElementById("workout-batches").addEventListener("toggle", (e) => {
    if (e.target.open) loadWorkoutBatches();
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
document.getElementById("btn-benchmarks-refresh").addEventListener("click", fetchBenchmarks);
document.getElementById("btn-zones-refresh").addEventListener("click", fetchZones);

/** Sport filters are built from the sports actually planned, so a newly-canonical sport
 *  appears here the moment it is used — the hardcoded <option> list this replaces had
 *  silently fallen behind the CLI's. */
async function populateSportFilters() {
    try {
        const res = await fetch(`${API_BASE}/api/workouts`);
        if (!res.ok) return;
        const workouts = await res.json();
        const sports = Array.from(new Set(workouts.map(w => w.sport_type).filter(Boolean))).sort();
        populateSportFilter(document.getElementById("wfilter-sport"), sports);
        populateSportFilter(document.getElementById("cmp-sport"), sports.filter(s => s !== "rest"));
    } catch (e) { /* filters simply stay at "All" */ }
}

// --- INITIALIZATION ---
document.addEventListener("DOMContentLoaded", () => {
    const wf = document.getElementById("wfilter-from");
    if (wf) wf.value = todayStr();

    fetchStatus();
    fetchObjectives();
    fetchEvents();
    fetchModels();
    populateSportFilters();
});
