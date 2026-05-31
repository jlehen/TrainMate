// --- CONSTANTS & DOM REFERENCES ---
const API_BASE = "";

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
            goalTitleEl.innerText = `${data.next_goal.title} (${data.next_goal.sport_type.replace('_', ' ').toUpperCase()})`;
            
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
        
    } catch (e) {
        logConsole(`Error fetching status: ${e.message}`, "error");
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
            const item = document.createElement("div");
            item.className = "list-item";
            item.innerHTML = `
                <div class="item-info">
                    <span class="item-title">${g.title} (${g.sport_type.replace('_', ' ')})</span>
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

document.getElementById("btn-sync-sheets").addEventListener("click", async () => {
    logConsole("Syncing Garmin metrics from Google Sheets...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/sync-sheets`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole("Garmin sheets sync complete!");
            fetchStatus();
        } else {
            logConsole(`Sync sheets failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`Sheets sync error: ${e.message}`, "error");
    }
});

document.getElementById("btn-replan").addEventListener("click", async () => {
    logConsole("Requesting Coach to generate periodized plan...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/plan`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Plan generated! ${data.workouts_count} workouts scheduled.`);
            logConsole(`Coach Reasoning:\n${data.reasoning}`, "system");
            fetchStatus();
            fetchWorkouts();
        } else {
            logConsole(`Coaching generation failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`AI Coaching Error: ${e.message}`, "error");
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

document.getElementById("btn-sync-calendar").addEventListener("click", async () => {
    logConsole("Syncing planned training sessions to Google Calendar...", "system");
    try {
        const res = await fetch(`${API_BASE}/api/sync`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            logConsole(`Synced successfully! ${data.synced_count} workouts written to Google Calendar.`);
            fetchWorkouts();
        } else {
            logConsole(`Calendar sync failed: ${data.error}`, "error");
        }
    } catch (e) {
        logConsole(`Calendar Sync Error: ${e.message}`, "error");
    }
});

// Forms
document.getElementById("form-add-goal").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = document.getElementById("goal-title").value;
    const target_date = document.getElementById("goal-date").value;
    const sport_type = document.getElementById("goal-sport").value;
    const priority = document.getElementById("goal-priority").value;
    const description = document.getElementById("goal-desc").value;
    
    logConsole(`Adding goal: ${title}...`, "system");
    try {
        const res = await fetch(`${API_BASE}/api/objectives`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, target_date, sport_type, priority, description })
        });
        if (res.ok) {
            logConsole(`Goal '${title}' added!`);
            document.getElementById("form-add-goal").reset();
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

document.getElementById("btn-clear-logs").addEventListener("click", () => {
    const consoleLogs = document.getElementById("console-logs");
    if (consoleLogs) consoleLogs.innerHTML = "";
});

// --- INITIALIZATION ---
document.addEventListener("DOMContentLoaded", () => {
    fetchStatus();
    fetchObjectives();
    fetchEvents();
    fetchWorkouts();
});
