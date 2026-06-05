// --- CONSTANTS & DOM REFERENCES ---
const API_BASE = "";
let activeMacrocycleId = null;
let activeMesocycleId = null;

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

// Switch tabs for objectives vs life events
window.switchTab = function(tabId) {
    document.querySelectorAll(".tab-content").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".tab-btn").forEach(el => el.classList.remove("active"));
    
    document.getElementById(tabId).classList.add("active");
    // Find matching button by icon or title
    const btn = Array.from(document.querySelectorAll(".tab-btn")).find(
        b => (tabId === "tab-goals" && b.innerText.includes("Objectives")) ||
             (tabId === "tab-events" && b.innerText.includes("Life Events"))
    );
    if (btn) btn.classList.add("active");
};

// --- API ACTIONS ---

async function fetchStatus() {
    try {
        const res = await fetch(`${API_BASE}/api/status`);
        if (!res.ok) throw new Error("Failed to load status");
        const data = await res.json();
        
        // Update Next Goal Header
        const goalTitleEl = document.getElementById("header-goal-title");
        const goalCountdownEl = document.getElementById("header-goal-countdown");
        if (data.next_goal) {
            const sportsList = data.next_goal.sport_type
                .split(',')
                .map(s => s.trim().replace('_', ' ').toUpperCase())
                .join(', ');
            goalTitleEl.innerText = `${data.next_goal.title} (${sportsList})`;
            
            // Calculate countdown
            const target = new Date(data.next_goal.target_date);
            const today = new Date();
            const diffTime = target - today;
            const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
            
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
            goalTitleEl.innerText = "No Active Goal";
            goalCountdownEl.innerText = "Add a goal to start planning";
            goalCountdownEl.style.color = "var(--text-muted)";
        }
        
        // Update Garmin Metrics and Baselines
        updateMetrics(data.last_metrics, data.last_baseline);
        
        // Update Coach Memory
        const strategyEl = document.getElementById("memory-strategy");
        const learningsEl = document.getElementById("memory-learnings");
        strategyEl.innerText = data.coach_memory.strategy || "No strategy established yet. Replan to generate one.";
        learningsEl.innerText = data.coach_memory.learnings || "No observations cached yet.";
        
        // Update Strategy Card
        const strategyCard = document.getElementById("strategy-card");
        if (data.macrocycle && data.mesocycles && data.mesocycles.length > 0) {
            activeMacrocycleId = data.macrocycle.id;
            strategyCard.style.display = "block";
            document.getElementById("strategy-philosophy").innerText = data.macrocycle.strategy;
            document.getElementById("macro-feedback-input").value = data.macrocycle.feedback || "";
            document.getElementById("macro-feedback-notice").style.display = "none";
            renderTimeline(data.mesocycles);
        } else {
            activeMacrocycleId = null;
            strategyCard.style.display = "none";
        }
        
        // Update Config Mismatch Warning Banner
        const banner = document.getElementById("config-warning-banner");
        if (banner) {
            if (data.config_mismatch) {
                banner.style.display = "flex";
                logConsole(
                    "Warning: config.yaml has changed since the active " +
                    "periodization plan was generated. Run 'Generate Plan' to update.",
                    "warning"
                );
            } else {
                banner.style.display = "none";
            }
        }
        
    } catch (e) {
        logConsole(`Error fetching status: ${e.message}`, "error");
    }
}

function parseLocalDate(dateStr) {
    const parts = dateStr.split("-");
    return new Date(parts[0], parts[1] - 1, parts[2]);
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
    
    // Sort mesocycles chronologically
    mesocycles.sort((a, b) => parseLocalDate(a.start_date) - parseLocalDate(b.start_date));
    
    // Calculate total duration in days
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
        
        // Determine cycle status
        let status = "future";
        if (end < today) {
            status = "done";
        } else if (start <= today && today <= end) {
            status = "active";
        }
        block.classList.add(status);
        
        block.addEventListener("click", () => {
            document.querySelectorAll(".cycle-block").forEach(el => el.classList.remove("selected"));
            block.classList.add("selected");
            
            detailsBox.style.display = "block";
            detailsName.innerText = m.name;
            detailsDates.innerText = `${m.start_date} to ${m.end_date} (${duration} days)`;
            detailsFocus.innerText = m.focus;
            
            // Populate feedback input
            activeMesocycleId = m.id;
            document.getElementById("meso-feedback-input").value = m.feedback || "";
            document.getElementById("meso-feedback-notice").style.display = "none";
        });
        
        container.appendChild(block);
        
        if (status === "active") {
            activeBlockEl = block;
        }
    });
    
    // Auto-select active or first block
    const defaultSelect = activeBlockEl || container.firstElementChild;
    if (defaultSelect) {
        defaultSelect.click();
    }
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
            // Assess status: HRV drops below baseline - 1.0 SD -> Amber, below -1.5 SD -> Red
            const diff = metrics.hrv - baseline.hrv_baseline_mean;
            const std = baseline.hrv_baseline_std || 5.0;
            
            hrvBadge.style.display = "inline-block";
            if (diff < -1.5 * std) {
                hrvBadge.className = "status-badge badge badge-danger";
                hrvBadge.innerText = "Suppressed";
            } else if (diff < -1.0 * std) {
                hrvBadge.className = "status-badge badge badge-warning";
                hrvBadge.innerText = "Fatigued";
            } else {
                hrvBadge.className = "status-badge badge badge-success";
                hrvBadge.innerText = "Balanced";
            }
        } else {
            hrvBase.innerText = "baseline: N/A";
            hrvBadge.style.display = "none";
        }
    } else {
        hrvVal.innerText = "--";
        hrvBase.innerText = "baseline: --";
        hrvBadge.style.display = "none";
    }
    
    // 2. RHR
    const rhrVal = document.querySelector("#metric-rhr .metric-value");
    const rhrBase = document.querySelector("#metric-rhr .metric-baseline");
    const rhrBadge = document.getElementById("rhr-badge");
    
    if (metrics && metrics.rhr) {
        rhrVal.innerHTML = `${metrics.rhr} <span class="unit">bpm</span>`;
        if (baseline && baseline.rhr_baseline_mean) {
            rhrBase.innerText = `baseline: ${baseline.rhr_baseline_mean.toFixed(1)} bpm`;
            // Assess status: RHR > mean + 3 bpm or > 1.0 SD -> amber, > 2.0 SD -> red
            const diff = metrics.rhr - baseline.rhr_baseline_mean;
            const std = baseline.rhr_baseline_std || 2.5;
            
            rhrBadge.style.display = "inline-block";
            if (diff > 2.0 * std || diff >= 5) {
                rhrBadge.className = "status-badge badge badge-danger";
                rhrBadge.innerText = "Elevated";
            } else if (diff > 1.0 * std || diff >= 3) {
                rhrBadge.className = "status-badge badge badge-warning";
                rhrBadge.innerText = "Stressed";
            } else {
                rhrBadge.className = "status-badge badge badge-success";
                rhrBadge.innerText = "Normal";
            }
        } else {
            rhrBase.innerText = "baseline: N/A";
            rhrBadge.style.display = "none";
        }
    } else {
        rhrVal.innerText = "--";
        rhrBase.innerText = "baseline: --";
        rhrBadge.style.display = "none";
    }
    
    // 3. Sleep
    const sleepVal = document.querySelector("#metric-sleep .metric-value");
    const sleepBase = document.querySelector("#metric-sleep .metric-baseline");
    const sleepBadge = document.getElementById("sleep-badge");
    
    if (metrics && metrics.sleep_score) {
        sleepVal.innerHTML = `${metrics.sleep_score} <span class="unit">/100</span>`;
        if (baseline && baseline.sleep_baseline_mean) {
            sleepBase.innerText = `baseline: ${baseline.sleep_baseline_mean.toFixed(1)}`;
            
            sleepBadge.style.display = "inline-block";
            if (metrics.sleep_score < 60) {
                sleepBadge.className = "status-badge badge badge-danger";
                sleepBadge.innerText = "Poor";
            } else if (metrics.sleep_score < 75) {
                sleepBadge.className = "status-badge badge badge-warning";
                sleepBadge.innerText = "Fair";
            } else {
                sleepBadge.className = "status-badge badge badge-success";
                sleepBadge.innerText = "Good";
            }
        } else {
            sleepBase.innerText = "baseline: N/A";
            sleepBadge.style.display = "none";
        }
    } else {
        sleepVal.innerText = "--";
        sleepBase.innerText = "baseline: --";
        sleepBadge.style.display = "none";
    }
    
    // 4. ACWR
    const acwrVal = document.querySelector("#metric-acwr .metric-value");
    const acwrBase = document.querySelector("#metric-acwr .metric-baseline");
    const acwrBadge = document.getElementById("acwr-badge");
    
    if (metrics && metrics.acwr !== null) {
        acwrVal.innerText = metrics.acwr.toFixed(2);
        acwrBase.innerText = `acute: ${metrics.acute_workload.toFixed(0)} | chronic: ${metrics.chronic_workload.toFixed(0)}`;
        
        acwrBadge.style.display = "inline-block";
        if (metrics.acwr > 1.5) {
            acwrBadge.className = "status-badge badge badge-danger";
            acwrBadge.innerText = "Danger";
        } else if (metrics.acwr > 1.3 || metrics.acwr < 0.8) {
            acwrBadge.className = "status-badge badge badge-warning";
            acwrBadge.innerText = metrics.acwr > 1.3 ? "Overload" : "Detraining";
        } else {
            acwrBadge.className = "status-badge badge badge-success";
            acwrBadge.innerText = "Sweet Spot";
        }
    } else {
        acwrVal.innerText = "--";
        acwrBase.innerText = "acute: -- | chronic: --";
        acwrBadge.style.display = "none";
    }
}

async function fetchObjectives() {
    try {
        const res = await fetch(`${API_BASE}/api/objectives`);
        if (!res.ok) throw new Error();
        const goals = await res.json();
        
        const container = document.getElementById("goals-list");
        container.innerHTML = "";
        
        if (goals.length === 0) {
            container.innerHTML = `<div class="item-meta" style="padding:0.5rem;">No active goals. Add one below to start planning.</div>`;
            return;
        }
        
        goals.forEach(g => {
            const sportsList = g.sport_type
                .split(',')
                .map(s => s.trim().replace('_', ' '))
                .join(', ');
            const item = document.createElement("div");
            item.className = "list-item";
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-title">${g.title} (${sportsList})</span>
                    <span class="item-meta">Target: ${g.target_date} | Priority: ${g.priority}</span>
                </div>
                <button class="btn-icon-only" onclick="deleteObjective(${g.id})"><i class="fa-solid fa-trash-can"></i></button>
            `;
            container.appendChild(item);
        });
    } catch (e) {
        logConsole("Failed to load goals", "error");
    }
}

async function deleteObjective(id) {
    if (!confirm("Are you sure you want to delete this objective?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/objectives/${id}`, { method: "DELETE" });
        if (res.ok) {
            logConsole("Objective deleted successfully.");
            fetchObjectives();
            fetchStatus();
        }
    } catch (e) {
        logConsole("Failed to delete objective", "error");
    }
}

async function fetchEvents() {
    try {
        const res = await fetch(`${API_BASE}/api/life-events`);
        if (!res.ok) throw new Error();
        const events = await res.json();
        
        const container = document.getElementById("events-list");
        container.innerHTML = "";
        
        if (events.length === 0) {
            container.innerHTML = `<div class="item-meta" style="padding:0.5rem;">No upcoming life events. Log travel or injuries.</div>`;
            return;
        }
        
        events.forEach(e => {
            const item = document.createElement("div");
            item.className = "list-item";
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-title">${e.title} (${e.event_type})</span>
                    <span class="item-meta">${e.start_date} to ${e.end_date}</span>
                </div>
                <button class="btn-icon-only" onclick="deleteEvent(${e.id})"><i class="fa-solid fa-trash-can"></i></button>
            `;
            container.appendChild(item);
        });
    } catch (e) {
        logConsole("Failed to load events", "error");
    }
}

async function deleteEvent(id) {
    if (!confirm("Delete this life event?")) return;
    try {
        const res = await fetch(`${API_BASE}/api/life-events/${id}`, { method: "DELETE" });
        if (res.ok) {
            logConsole("Life event deleted.");
            fetchEvents();
        }
    } catch (e) {
        logConsole("Failed to delete event", "error");
    }
}

async function fetchWorkouts() {
    try {
        const todayStr = new Date().toISOString().split("T")[0];
        const res = await fetch(`${API_BASE}/api/workouts?start_date=${todayStr}`);
        if (!res.ok) throw new Error();
        const workouts = await res.json();
        
        const container = document.getElementById("workouts-list-container");
        container.innerHTML = "";
        
        const badgeEl = document.getElementById("workout-count");
        badgeEl.innerText = `${workouts.length} workouts scheduled`;
        
        if (workouts.length === 0) {
            container.innerHTML = `<div class="item-meta" style="text-align:center; padding: 2rem;">No workouts scheduled. Add a goal and click "Ask Coach to Replan".</div>`;
            return;
        }
        
        workouts.forEach(w => {
            const dateObj = new Date(w.date);
            const dayNum = dateObj.getUTCDate();
            const monthStr = dateObj.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
            const weekdayStr = dateObj.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
            
            const isAdapted = w.status === 'modified' || w.modification_reason;
            const isSynced = w.status === 'synced';
            
            let cardClass = "workout-card";
            if (isAdapted) cardClass += " adapted";
            else if (isSynced) cardClass += " synced";
            
            let iconClass = "workout-sport-icon";
            let iconGlyph = "fa-person-running";
            
            if (w.sport_type === "road_biking") {
                iconClass += " road_biking";
                iconGlyph = "fa-bicycle";
            } else if (w.sport_type === "hiking") {
                iconClass += " hiking";
                iconGlyph = "fa-mountain-sun";
            } else if (w.sport_type === "strength_training") {
                iconClass += " strength_training";
                iconGlyph = "fa-dumbbell";
            } else if (w.sport_type === "running") {
                iconClass += " running";
            } else if (w.sport_type === "yoga") {
                iconClass += " yoga";
                iconGlyph = "fa-spa";
            } else if (w.sport_type === "ski_touring") {
                iconClass += " ski_touring";
                iconGlyph = "fa-person-skiing-nordic";
            } else {
                iconClass += " rest";
                iconGlyph = "fa-bed";
            }
            
            const item = document.createElement("div");
            item.className = cardClass;
            
            let reasonHtml = "";
            if (isAdapted && w.modification_reason) {
                reasonHtml = `<div class="workout-reason"><i class="fa-solid fa-triangle-exclamation"></i> Adapted Reason: ${w.modification_reason}</div>`;
            }
            
            let origHtml = "";
            if (isAdapted && w.original_description && w.original_description !== w.description) {
                origHtml = `<div class="workout-orig"><i class="fa-solid fa-history"></i> Originally: ${w.original_description}</div>`;
            }
            
            item.innerHTML = `
                <div class="workout-date">
                    <span class="workout-day">${dayNum}</span>
                    <span class="workout-month">${monthStr}</span>
                    <span class="workout-weekday">${weekdayStr}</span>
                </div>
                <div class="workout-body">
                    <span class="workout-title">${w.title}</span>
                    <span class="workout-desc">${w.description}</span>
                    ${reasonHtml}
                    ${origHtml}
                </div>
                <div class="${iconClass}">
                    <i class="fa-solid ${iconGlyph}"></i>
                </div>
            `;
            container.appendChild(item);
        });
        
    } catch (e) {
        logConsole("Failed to load workouts", "error");
    }
}

// --- BUTTON TRIGGER FUNCTIONS ---

document.getElementById("btn-pull-metrics").addEventListener("click", async () => {
    logConsole("Pulling Garmin metrics from Google Sheets...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/metrics/pull`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole("Garmin metrics pull complete!");
            fetchStatus();
        } else {
            logConsole(`Pull metrics failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`Pull metrics error: ${e.message}`, "error");
    }
});

document.getElementById("btn-generate-plan").addEventListener("click", async () => {
    logConsole("Requesting Coach to generate periodized plan strategy (macro/meso)...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/plan`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Periodization plan generated! ${data.mesocycles_count} mesocycles established.`);
            if (data.strategy) {
                logConsole(`Overall Strategy:\n${data.strategy}`, "system");
            }
            fetchStatus();
        } else {
            logConsole(`Plan generation failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`AI Planning Error: ${e.message}`, "error");
    }
});

document.getElementById("btn-generate-workouts").addEventListener("click", async () => {
    logConsole("Requesting Coach to generate workouts (microcycles)...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/workouts/generate`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Workouts generated! ${data.workouts_count} workouts scheduled.`);
            if (data.reasoning) {
                logConsole(`Coach Reasoning:\n${data.reasoning}`, "system");
            }
            fetchWorkouts();
        } else {
            logConsole(`Workout generation failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`AI Workout Generation Error: ${e.message}`, "error");
    }
});

document.getElementById("btn-adapt").addEventListener("click", async () => {
    logConsole("Running daily Garmin metrics adaptation check...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/adapt`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Daily Check Complete: ${data.reason}`, "system");
            if (data.adapted) {
                logConsole(`Workout was adapted: ${data.workout.title}`, "warning");
            } else {
                logConsole(`Workout remains as scheduled.`, "system");
            }
            fetchStatus();
            fetchWorkouts();
        } else {
            logConsole(`Adaptation check failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`Adaptation check error: ${e.message}`, "error");
    }
});

document.getElementById("btn-push-workouts").addEventListener("click", async () => {
    logConsole("Pushing planned training sessions to Google Calendar...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/workouts/push`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Pushed successfully! ${data.synced_count} workouts written to Google Calendar.`);
            fetchWorkouts();
        } else {
            logConsole(`Calendar push failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`Calendar Push Error: ${e.message}`, "error");
    }
});

// Forms
document.getElementById("form-add-goal").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = document.getElementById("goal-title").value;
    const target_date = document.getElementById("goal-date").value;
    
    // Get selected sports from chips
    const selectedChips = document.querySelectorAll("#goal-sports-chips .sport-chip.selected");
    const sports = Array.from(selectedChips).map(c => c.dataset.value);
    if (sports.length === 0) {
        logConsole("Failed to add goal: Please select at least one sport.", "error");
        alert("Please select at least one sport.");
        return;
    }
    
    const priority = document.getElementById("goal-priority").value;
    const description = document.getElementById("goal-desc").value;
    
    logConsole(`Adding goal: ${title}...`, "system");
    try {
        const res = await fetch(`${API_BASE}/api/objectives`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, target_date, sport_type: sports, priority, description })
        });
        if (res.ok) {
            logConsole(`Goal '${title}' added!`);
            document.getElementById("form-add-goal").reset();
            selectedChips.forEach(c => c.classList.remove("selected"));
            fetchObjectives();
            fetchStatus();
        } else {
            const data = await res.json();
            logConsole(`Add goal failed: ${data.error}`, "error");
        }
    } catch (err) {
        logConsole(`Error adding goal: ${err.message}`, "error");
    }
});

document.getElementById("form-add-event").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = document.getElementById("event-title").value;
    const start_date = document.getElementById("event-start").value;
    const end_date = document.getElementById("event-end").value;
    const event_type = document.getElementById("event-type").value;
    const impact_description = document.getElementById("event-desc").value;
    
    logConsole(`Logging life event: ${title}...`, "system");
    try {
        const res = await fetch(`${API_BASE}/api/life-events`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, start_date, end_date, event_type, impact_description })
        });
        if (res.ok) {
            logConsole(`Logged life event '${title}'!`);
            document.getElementById("form-add-event").reset();
            fetchEvents();
        } else {
            const data = await res.json();
            logConsole(`Log event failed: ${data.error}`, "error");
        }
    } catch (err) {
        logConsole(`Error logging event: ${err.message}`, "error");
    }
});

document.getElementById("btn-save-macro-feedback").addEventListener("click", async () => {
    if (!activeMacrocycleId) {
        logConsole("No active macrocycle to save feedback for.", "error");
        return;
    }
    const feedback = document.getElementById("macro-feedback-input").value;
    logConsole("Saving strategy feedback...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/macrocycles/${activeMacrocycleId}/feedback`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feedback })
        });
        const data = await res.json();
        if (res.ok) {
            logConsole("Strategy feedback saved successfully.");
            document.getElementById("macro-feedback-notice").style.display = "block";
            // Refresh status to ensure local data is updated
            const statusRes = await fetch(`${API_BASE}/api/status`);
            if (statusRes.ok) {
                const statusData = await statusRes.json();
                if (statusData.macrocycle) {
                    document.getElementById("macro-feedback-input").value =
                        statusData.macrocycle.feedback || "";
                }
            }
        } else {
            logConsole(`Failed to save strategy feedback: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`Error saving strategy feedback: ${e.message}`, "error");
    }
});

document.getElementById("btn-save-meso-feedback").addEventListener("click", async () => {
    if (!activeMesocycleId) {
        logConsole("No active mesocycle to save feedback for.", "error");
        return;
    }
    const feedback = document.getElementById("meso-feedback-input").value;
    logConsole("Saving block feedback...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/mesocycles/${activeMesocycleId}/feedback`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feedback })
        });
        const data = await res.json();
        if (res.ok) {
            logConsole("Block feedback saved successfully.");
            document.getElementById("meso-feedback-notice").style.display = "block";
            // Refresh status to update local mesocycles list without losing active selection
            const savedSelectedId = activeMesocycleId;
            const statusRes = await fetch(`${API_BASE}/api/status`);
            if (statusRes.ok) {
                const statusData = await statusRes.json();
                // Redraw timeline
                renderTimeline(statusData.mesocycles);
                // Reselect the block that was updated
                const blocks = document.querySelectorAll(".cycle-block");
                blocks.forEach(b => {
                    const blockTitle = b.title;
                    const targetMeso = statusData.mesocycles.find(item => item.id === savedSelectedId);
                    if (targetMeso && blockTitle.includes(targetMeso.name) &&
                        blockTitle.includes(targetMeso.start_date)) {
                        b.click();
                    }
                });
            }
        } else {
            logConsole(`Failed to save block feedback: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`Error saving block feedback: ${e.message}`, "error");
    }
});

document.getElementById("btn-clear-logs").addEventListener("click", () => {
    const consoleLogs = document.getElementById("console-logs");
    if (consoleLogs) consoleLogs.innerHTML = "";
});

// --- INITIALIZATION ---
document.addEventListener("DOMContentLoaded", () => {
    // Setup interactive sport chips
    document.querySelectorAll("#goal-sports-chips .sport-chip").forEach(chip => {
        chip.addEventListener("click", () => {
            chip.classList.toggle("selected");
        });
    });
    
    fetchStatus();
    fetchObjectives();
    fetchEvents();
    fetchWorkouts();
});
