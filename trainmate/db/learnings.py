from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Coach-learning enrichment (Phase 2).
# Ordered confidence levels the LLM assigns to each observation.
CONFIDENCE_LEVELS = ("tentative", "moderate", "established")

# A learning is "dormant" — kept in the DB but excluded from LLM prompts — once it
# has gone unreinforced for longer than the budget for its confidence level. Decay is
# soft: a dormant learning revives the moment it is reinforced again.
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


class LearningsMixin:
    """Coach learnings: discrete, addressable athlete-observation records."""

    def get_learnings(self) -> List[Dict[str, Any]]:
        """Returns all athlete-observation records ordered by id. Each record carries a
        computed `dormant` flag (True once it has decayed past its confidence budget)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, text, sports, confidence, created_at, updated_at, "
                "last_reinforced_at FROM coach_learnings ORDER BY id"
            )
            learnings = [dict(row) for row in cursor.fetchall()]
        now = datetime.now(timezone.utc)
        for learning in learnings:
            learning["dormant"] = learning_is_dormant(learning, now)
        return learnings

    def add_learning(
        self, text: str, sports: str = "general", confidence: str = "tentative"
    ) -> int:
        """Adds a single athlete-observation record and returns its id."""
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
            return cursor.lastrowid

    def update_learning(self, learning_id: int, text: str) -> None:
        """Revises the text of an existing observation record."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET text=?, updated_at=? WHERE id=?",
                (text, now, learning_id)
            )

    def delete_learning(self, learning_id: int) -> None:
        """Removes an observation record."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM coach_learnings WHERE id=?", (learning_id,))

    def apply_learning_deltas(
        self, deltas: List[Dict[str, Any]], suppress_reinforcement: bool = False
    ) -> None:
        """Applies a list of incremental learning operations in one transaction.

        Each delta is one of:
          {"op": "add", "text": "...", "sports"?: "...", "confidence"?: "..."}
          {"op": "revise", "id": <int>, "text"?: "...", "sports"?: "...", "confidence"?: "..."}
          {"op": "reinforce", "id": <int>, "confidence"?: "..."}
          {"op": "retire", "id": <int>}
        `add` defaults sports to 'general' and confidence to 'tentative'. `revise` and
        `reinforce` refresh recency (last_reinforced_at), so a reaffirmed learning leaves
        the dormant state. Invalid confidence values and malformed deltas (empty text,
        missing id, unknown op) are skipped so a partially-valid response still applies.

        Reinforcement integrity invariant (see DESIGN_backward_evaluation.md §8). When
        `suppress_reinforcement` is True — i.e. these deltas were derived from *unchanged*
        evidence (a forced re-run over a window whose fingerprint already produced
        learnings) — the purely-ratcheting effects are dropped so re-reading the same data
        cannot inflate confidence or reset the decay clock:
          - `reinforce` ops are skipped entirely.
          - `revise` ops still apply content (text/sports/confidence) but do NOT refresh
            `last_reinforced_at`.
          - `add` and `retire` are unaffected: a learning newly surfaced or retired from
            the same evidence is genuinely new knowledge, not a double-count.
        """
        if not deltas:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for delta in deltas:
                if not isinstance(delta, dict):
                    continue
                op = delta.get("op")
                if op == "add":
                    text = (delta.get("text") or "").strip()
                    if text:
                        cursor.execute(
                            "INSERT INTO coach_learnings (text, sports, confidence, "
                            "created_at, updated_at, last_reinforced_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (text, normalize_sports(delta.get("sports")),
                             valid_confidence(delta.get("confidence")) or "tentative",
                             now, now, now)
                        )
                elif op == "revise":
                    learning_id = delta.get("id")
                    if learning_id is None:
                        continue
                    # Apply only the fields the model supplied. A revise normally reflects
                    # fresh evidence, so it refreshes recency — but not when the evidence is
                    # unchanged (suppress_reinforcement), where only the content edit stands.
                    sets, params = [], []
                    text = (delta.get("text") or "").strip()
                    if text:
                        sets.append("text=?")
                        params.append(text)
                    if delta.get("sports"):
                        sets.append("sports=?")
                        params.append(normalize_sports(delta.get("sports")))
                    confidence = valid_confidence(delta.get("confidence"))
                    if confidence:
                        sets.append("confidence=?")
                        params.append(confidence)
                    if not sets:
                        continue
                    sets.append("updated_at=?")
                    params.append(now)
                    if not suppress_reinforcement:
                        sets.append("last_reinforced_at=?")
                        params.append(now)
                    params.append(learning_id)
                    cursor.execute(
                        f"UPDATE coach_learnings SET {', '.join(sets)} WHERE id=?", params
                    )
                elif op == "reinforce":
                    # Pure ratchet: the only spurious op on unchanged evidence.
                    if suppress_reinforcement:
                        continue
                    learning_id = delta.get("id")
                    if learning_id is None:
                        continue
                    sets, params = ["last_reinforced_at=?"], [now]
                    confidence = valid_confidence(delta.get("confidence"))
                    if confidence:
                        sets += ["confidence=?", "updated_at=?"]
                        params += [confidence, now]
                    params.append(learning_id)
                    cursor.execute(
                        f"UPDATE coach_learnings SET {', '.join(sets)} WHERE id=?", params
                    )
                elif op == "retire":
                    learning_id = delta.get("id")
                    if learning_id is not None:
                        cursor.execute(
                            "DELETE FROM coach_learnings WHERE id=?", (learning_id,)
                        )
