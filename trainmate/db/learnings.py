from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from trainmate.config import config

# Ordered confidence levels, weakest → strongest. Confidence is now APP-COMPUTED from each
# learning's evidence basis (see derive_confidence) rather than LLM-asserted.
CONFIDENCE_LEVELS = ("tentative", "moderate", "established")

# Sentinel stored in `proposed_confidence` when the pending downgrade is a *retirement*
# (net support fell to ≤0 under contradiction, or a tentative learning aged out). Not a
# real confidence level, so it never validates as one.
RETIRE_PROPOSAL = "retire"

# A learning is "dormant" — kept in the DB but excluded from LLM prompts — once it has gone
# unreinforced for longer than the budget for its confidence level. Decay is soft: a dormant
# learning revives the moment new supporting evidence lands (or a staleness demotion re-arms
# its clock). Crossing the budget also proposes a one-level staleness demotion (§7/§8).
LEARNING_STALENESS_DAYS = {
    "tentative": 21,
    "moderate": 60,
    "established": 180,
}


def normalize_sports(value: Any) -> str:
    """Normalizes a sport-scope value (list or comma string) to a comma-joined,
    lowercased string; empty/missing becomes 'general'."""
    if not value:
        return "general"
    if isinstance(value, (list, tuple)):
        parts = [str(s).strip().lower() for s in value if str(s).strip()]
    else:
        parts = [s.strip().lower() for s in str(value).split(",") if s.strip()]
    return ",".join(parts) if parts else "general"


def valid_confidence(value: Any) -> Optional[str]:
    """Returns the confidence value if it is a recognized level, else None."""
    return value if value in CONFIDENCE_LEVELS else None


def confidence_rank(value: Any) -> int:
    """Ordinal rank for comparing levels. RETIRE_PROPOSAL / unknown rank below tentative."""
    try:
        return CONFIDENCE_LEVELS.index(value) + 1
    except ValueError:
        return 0  # retire / unknown — below tentative


def step_down(level: str) -> str:
    """The next confidence level *down*, or RETIRE_PROPOSAL below tentative."""
    rank = confidence_rank(level)
    if rank <= 1:
        return RETIRE_PROPOSAL
    return CONFIDENCE_LEVELS[rank - 2]


def derive_confidence(
    supporting_weeks: int, contradicting_weeks: int,
    thresholds: Optional[Dict[str, int]] = None
) -> str:
    """Maps an evidence basis to a confidence level (DESIGN_evidence_based_confidence.md §3).

    `net = supporting − contradicting`. Returns RETIRE_PROPOSAL only when contradiction has
    actually driven net ≤ 0 — a learning with *no* basis yet (net 0, nothing contradicting)
    rests at the tentative floor rather than being proposed for retirement.
    """
    thresholds = thresholds or config.learning_confidence_thresholds
    net = supporting_weeks - contradicting_weeks
    if net >= thresholds.get("established", 5):
        return "established"
    if net >= thresholds.get("moderate", 3):
        return "moderate"
    if net >= 1:
        return "tentative"
    if contradicting_weeks > 0:
        return RETIRE_PROPOSAL
    return "tentative"


def learning_is_dormant(learning: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True if a learning has gone unreinforced past its confidence-based budget."""
    now = now or datetime.now(timezone.utc)
    ref = learning.get("last_reinforced_at") or learning.get("created_at")
    if not ref:
        return False
    try:
        ref_dt = datetime.fromisoformat(ref)
    except (ValueError, TypeError):
        return False
    budget = LEARNING_STALENESS_DAYS.get(
        learning.get("confidence") or "tentative", LEARNING_STALENESS_DAYS["tentative"]
    )
    return (now - ref_dt) > timedelta(days=budget)


def _monday_str(date_str: str) -> Optional[str]:
    """Returns the Monday (ISO week start) for a YYYY-MM-DD string, or None if unparseable."""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


class LearningsMixin:
    """Coach learnings: discrete, addressable athlete-observation records whose confidence
    is computed by the app from a per-learning evidence basis (the distinct training weeks
    that back each observation). See DESIGN_evidence_based_confidence.md."""

    # ------------------------------------------------------------------ reads

    def get_learnings(self) -> List[Dict[str, Any]]:
        """Returns all observation records ordered by id. Each carries a computed `dormant`
        flag (decayed past its budget) and its (nullable) `proposed_confidence` downgrade."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, text, sports, confidence, proposed_confidence, created_at, "
                "updated_at, last_reinforced_at FROM coach_learnings ORDER BY id"
            )
            learnings = [dict(row) for row in cursor.fetchall()]
        now = datetime.now(timezone.utc)
        for learning in learnings:
            learning["dormant"] = learning_is_dormant(learning, now)
        return learnings

    def get_learning_evidence(self, learning_id: int) -> List[Dict[str, Any]]:
        """Returns the evidence basis rows for a learning, ordered by week then polarity."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, learning_id, week_commencing, polarity, source, created_at "
                "FROM learning_evidence WHERE learning_id=? "
                "ORDER BY week_commencing, polarity",
                (learning_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------- basic CRUD

    def add_learning(
        self, text: str, sports: str = "general", confidence: str = "tentative"
    ) -> int:
        """Adds a single observation record and returns its id.

        A synthetic supporting basis sized to sustain the requested `confidence` is seeded
        (source 'manual'), so the app-computed recompute keeps the level instead of demoting
        it for lack of evidence — mirroring the grandfather migration (§9)."""
        now = datetime.now(timezone.utc).isoformat()
        sports = normalize_sports(sports)
        confidence = valid_confidence(confidence) or "tentative"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO coach_learnings (text, sports, confidence, created_at, "
                "updated_at, last_reinforced_at) VALUES (?, ?, ?, ?, ?, ?)",
                (text, sports, confidence, now, now, now)
            )
            lid = cursor.lastrowid
            self._seed_synthetic_evidence(cursor, lid, confidence, now, source="manual")
            return lid

    def update_learning(self, learning_id: int, text: str) -> None:
        """Revises the text of an existing observation record."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET text=?, updated_at=? WHERE id=?",
                (text, now, learning_id)
            )

    def delete_learning(self, learning_id: int) -> None:
        """Removes an observation record (its evidence basis cascades)."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM coach_learnings WHERE id=?", (learning_id,))

    # ----------------------------------------------------- evidence internals

    def _evidence_counts(self, cursor, learning_id: int) -> Tuple[int, int]:
        """(distinct supporting weeks, distinct contradicting weeks) for a learning."""
        cursor.execute(
            "SELECT polarity, COUNT(DISTINCT week_commencing) FROM learning_evidence "
            "WHERE learning_id=? GROUP BY polarity",
            (learning_id,)
        )
        sup = con = 0
        for polarity, count in cursor.fetchall():
            if polarity >= 0:
                sup = count
            else:
                con = count
        return sup, con

    def _add_evidence_weeks(
        self, cursor, learning_id: int, weeks: Iterable[str], polarity: int,
        source: str, now: str
    ) -> int:
        """Inserts (week, polarity) evidence rows, deduped via INSERT-OR-IGNORE. Returns the
        number of *new* rows actually inserted (0 means every cited week was already counted)."""
        inserted = 0
        for raw in weeks:
            monday = _monday_str(raw) if isinstance(raw, str) else None
            if not monday:
                continue
            cursor.execute(
                "INSERT OR IGNORE INTO learning_evidence "
                "(learning_id, week_commencing, polarity, source, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (learning_id, monday, polarity, source, now)
            )
            inserted += cursor.rowcount
        return inserted

    def _seed_synthetic_evidence(
        self, cursor, learning_id: int, confidence: str, anchor_iso: str, source: str
    ) -> None:
        """Seeds enough distinct synthetic supporting weeks (stepping back from the anchor
        week) to sustain `confidence`, so a basis-derived recompute keeps the level (§9)."""
        thresholds = config.learning_confidence_thresholds
        need = {
            "established": thresholds.get("established", 5),
            "moderate": thresholds.get("moderate", 3),
            "tentative": 1,
        }.get(confidence, 1)
        anchor = _monday_str(anchor_iso) or _monday_str(
            datetime.now(timezone.utc).isoformat()
        )
        base = datetime.strptime(anchor, "%Y-%m-%d").date()
        weeks = [(base - timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(need)]
        self._add_evidence_weeks(cursor, learning_id, weeks, +1, source, anchor_iso)

    def _grandfather_learning_evidence(self) -> None:
        """One-time seed of a synthetic basis for learnings predating the evidence model
        (those with an empty basis). Idempotent: a learning gains ≥1 row here and is never
        revisited. See DESIGN_evidence_based_confidence.md §9."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT cl.id, cl.confidence, cl.created_at FROM coach_learnings cl "
                "LEFT JOIN learning_evidence le ON le.learning_id = cl.id "
                "WHERE le.id IS NULL"
            )
            rows = cursor.fetchall()
            for row in rows:
                self._seed_synthetic_evidence(
                    cursor, row["id"], row["confidence"] or "tentative",
                    row["created_at"], source="migration"
                )

    def _recompute_confidence(self, cursor, learning_id: int, now: str) -> None:
        """Re-derives a learning's confidence from its basis and applies the upgrade/propose
        rule (§3, §7): an upgrade (or unchanged) is applied immediately; a downgrade is only
        *proposed* (written to proposed_confidence), leaving the live level intact."""
        cursor.execute(
            "SELECT confidence, proposed_confidence FROM coach_learnings WHERE id=?",
            (learning_id,)
        )
        row = cursor.fetchone()
        if not row:
            return
        current = row["confidence"] or "tentative"
        sup, con = self._evidence_counts(cursor, learning_id)
        derived = derive_confidence(sup, con)
        if confidence_rank(derived) >= confidence_rank(current):
            # Earned upgrade (or no change): apply now, clear any pending downgrade.
            if derived != current or row["proposed_confidence"] is not None:
                cursor.execute(
                    "UPDATE coach_learnings SET confidence=?, proposed_confidence=NULL, "
                    "updated_at=? WHERE id=?",
                    (derived, now, learning_id)
                )
        else:
            # Downgrade: propose, don't apply.
            cursor.execute(
                "UPDATE coach_learnings SET proposed_confidence=?, updated_at=? WHERE id=?",
                (derived, now, learning_id)
            )

    # -------------------------------------------------------- delta application

    def apply_learning_deltas(
        self, deltas: List[Dict[str, Any]],
        available_weeks: Optional[Iterable[str]] = None, source: str = "reflect"
    ) -> None:
        """Applies evidence-cited learning operations in one transaction, then recomputes the
        confidence of every touched learning from its (updated) basis.

        Each delta is one of (DESIGN_evidence_based_confidence.md §6):
          {"op": "add", "text": "...", "sports"?: "...", "evidence"?: [weeks]}
          {"op": "revise", "id": <int>, "text"?: "...", "sports"?: "...", "evidence"?: [weeks]}
          {"op": "reinforce", "id": <int>, "evidence": [weeks]}   # no evidence ⇒ no-op
          {"op": "contradict", "id": <int>, "evidence": [weeks]}  # no evidence ⇒ no-op
          {"op": "retire", "id": <int>}

        The LLM no longer sets confidence — it only attributes observations to the
        `week_commencing` weeks it was shown. Cited weeks are normalized to their Monday and,
        when `available_weeks` is provided, validated against it (weeks outside the analysed
        window are dropped, mirroring the skip-malformed philosophy). `last_reinforced_at` is
        refreshed only when a *new* supporting week actually lands — so re-citing counted
        weeks neither inflates confidence nor resets decay. Malformed deltas are skipped.
        """
        if not deltas:
            return
        allowed: Optional[Set[str]] = None
        if available_weeks is not None:
            allowed = {
                m for m in (_monday_str(w) for w in available_weeks if isinstance(w, str))
                if m
            }
        now = datetime.now(timezone.utc).isoformat()

        def filtered(raw_weeks: Any) -> List[str]:
            if not isinstance(raw_weeks, (list, tuple)):
                return []
            out = []
            for w in raw_weeks:
                m = _monday_str(w) if isinstance(w, str) else None
                if m and (allowed is None or m in allowed):
                    out.append(m)
            return out

        def _exists(cursor, lid) -> bool:
            cursor.execute("SELECT 1 FROM coach_learnings WHERE id=?", (lid,))
            return cursor.fetchone() is not None

        touched: Set[int] = set()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for delta in deltas:
                if not isinstance(delta, dict):
                    continue
                op = delta.get("op")
                if op == "add":
                    text = (delta.get("text") or "").strip()
                    if not text:
                        continue
                    cursor.execute(
                        "INSERT INTO coach_learnings (text, sports, confidence, created_at, "
                        "updated_at, last_reinforced_at) VALUES (?, ?, 'tentative', ?, ?, ?)",
                        (text, normalize_sports(delta.get("sports")), now, now, now)
                    )
                    lid = cursor.lastrowid
                    self._add_evidence_weeks(
                        cursor, lid, filtered(delta.get("evidence")), +1, source, now
                    )
                    touched.add(lid)
                elif op == "revise":
                    lid = delta.get("id")
                    if lid is None or not _exists(cursor, lid):
                        continue  # skip hallucinated ids (FK would abort the txn)
                    sets, params = [], []
                    text = (delta.get("text") or "").strip()
                    if text:
                        sets.append("text=?")
                        params.append(text)
                    if delta.get("sports"):
                        sets.append("sports=?")
                        params.append(normalize_sports(delta.get("sports")))
                    if sets:
                        sets.append("updated_at=?")
                        params.append(now)
                        params.append(lid)
                        cursor.execute(
                            f"UPDATE coach_learnings SET {', '.join(sets)} WHERE id=?", params
                        )
                    new_weeks = self._add_evidence_weeks(
                        cursor, lid, filtered(delta.get("evidence")), +1, source, now
                    )
                    if new_weeks:
                        cursor.execute(
                            "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?",
                            (now, lid)
                        )
                    touched.add(lid)
                elif op == "reinforce":
                    lid = delta.get("id")
                    if lid is None or not _exists(cursor, lid):
                        continue
                    new_weeks = self._add_evidence_weeks(
                        cursor, lid, filtered(delta.get("evidence")), +1, source, now
                    )
                    # Recency follows evidence: only a genuinely new week keeps it fresh.
                    if new_weeks:
                        cursor.execute(
                            "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?",
                            (now, lid)
                        )
                        touched.add(lid)
                elif op == "contradict":
                    lid = delta.get("id")
                    if lid is None or not _exists(cursor, lid):
                        continue
                    new_weeks = self._add_evidence_weeks(
                        cursor, lid, filtered(delta.get("evidence")), -1, source, now
                    )
                    if new_weeks:
                        touched.add(lid)
                elif op == "retire":
                    lid = delta.get("id")
                    if lid is not None:
                        cursor.execute("DELETE FROM coach_learnings WHERE id=?", (lid,))

            # Re-level every touched learning from its (updated) basis in the same transaction
            # the denormalized confidence column lives in.
            for lid in touched:
                self._recompute_confidence(cursor, lid, now)

    def recompute_all_confidence(self) -> None:
        """Re-derives confidence for every learning from its basis (upgrade auto-applies,
        downgrade is proposed). Used to re-level after `learning_confidence_thresholds`
        changes — no migration needed (§3)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM coach_learnings")
            ids = [row["id"] for row in cursor.fetchall()]
            for lid in ids:
                self._recompute_confidence(cursor, lid, now)

    # ---------------------------------------------------- staleness & proposals

    def derive_staleness_proposals(self, auto: bool = False) -> None:
        """Proposes a one-level staleness demotion for each learning that has crossed into
        dormancy without one already pending (§7/§8).

        Interactive (`auto=False`): writes `proposed_confidence` (one level down, or
        RETIRE_PROPOSAL below tentative) for the human to confirm. Unattended (`auto=True`):
        applies the step directly and re-arms the dormancy clock at the new (shorter) budget,
        so an untouched learning walks down to retirement over real time rather than in one
        jump. A learning with a contradiction proposal already pending is left untouched.
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, confidence, proposed_confidence, created_at, last_reinforced_at "
                "FROM coach_learnings"
            )
            rows = [dict(r) for r in cursor.fetchall()]
            for row in rows:
                if row.get("proposed_confidence"):
                    continue  # a downgrade is already pending; don't escalate.
                if not learning_is_dormant(row, now_dt):
                    continue
                target = step_down(row["confidence"] or "tentative")
                if auto:
                    if target == RETIRE_PROPOSAL:
                        cursor.execute(
                            "DELETE FROM coach_learnings WHERE id=?", (row["id"],)
                        )
                    else:
                        # Apply + re-arm the clock at the lower level's budget.
                        cursor.execute(
                            "UPDATE coach_learnings SET confidence=?, last_reinforced_at=?, "
                            "updated_at=? WHERE id=?",
                            (target, now, now, row["id"])
                        )
                else:
                    cursor.execute(
                        "UPDATE coach_learnings SET proposed_confidence=?, updated_at=? "
                        "WHERE id=?",
                        (target, now, row["id"])
                    )

    def demote_learning(self, learning_id: int) -> Optional[str]:
        """Accepts a pending downgrade: writes confidence = proposed_confidence (or retires the
        learning on the retirement sentinel), clears the proposal, and re-arms the dormancy
        clock at the new level. Returns the new level, 'retired', or None if nothing pending."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT proposed_confidence FROM coach_learnings WHERE id=?", (learning_id,)
            )
            row = cursor.fetchone()
            if not row or not row["proposed_confidence"]:
                return None
            target = row["proposed_confidence"]
            if target == RETIRE_PROPOSAL:
                cursor.execute("DELETE FROM coach_learnings WHERE id=?", (learning_id,))
                return "retired"
            cursor.execute(
                "UPDATE coach_learnings SET confidence=?, proposed_confidence=NULL, "
                "last_reinforced_at=?, updated_at=? WHERE id=?",
                (target, now, now, learning_id)
            )
            return target

    def keep_learning(self, learning_id: int) -> None:
        """Dismisses + affirms a pending downgrade (§7). For a contradiction-driven proposal,
        neutralizes the −1 evidence rows (the human overrules the disconfirming weeks); for a
        staleness-driven one, refreshes `last_reinforced_at` (the affirmation counts as
        reinforcement). Either way clears the proposal and re-derives confidence."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT confidence, proposed_confidence FROM coach_learnings WHERE id=?",
                (learning_id,)
            )
            row = cursor.fetchone()
            if not row or not row["proposed_confidence"]:
                return
            current = row["confidence"] or "tentative"
            sup, con = self._evidence_counts(cursor, learning_id)
            contradiction_driven = (
                con > 0 and confidence_rank(derive_confidence(sup, con)) < confidence_rank(current)
            )
            if contradiction_driven:
                cursor.execute(
                    "DELETE FROM learning_evidence WHERE learning_id=? AND polarity < 0",
                    (learning_id,)
                )
            else:
                cursor.execute(
                    "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?",
                    (now, learning_id)
                )
            cursor.execute(
                "UPDATE coach_learnings SET proposed_confidence=NULL, updated_at=? WHERE id=?",
                (now, learning_id)
            )
            self._recompute_confidence(cursor, learning_id, now)
