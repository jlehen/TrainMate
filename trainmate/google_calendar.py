import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, List, Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from trainmate import runtime
from trainmate.config import config
from trainmate.types import Workout
from trainmate.adherence import STATUS_LABELS
from trainmate.calendar_state import calendar_signature
from trainmate import calendar_lineage
from trainmate import intensity
from trainmate.util import fmt_date, fmt_timestamp, step, warn

# Events fetched per Calendar API page during a signal sync (the response is paged
# through with pageToken regardless, so this only tunes round-trips vs payload size).
CALENDAR_SYNC_PAGE_SIZE = 250

# Tag stamped on every workout event we write, and the only handle on ownership left
# once the rows that referenced the events are gone (see `list_workout_events`).
WORKOUT_EVENT_TAG = "TrainMate"


# Whether each event write announces itself. Callers that push a whole batch render a
# count and a progress bar instead, and silence the per-event lines with `quiet_events()`.
_event_log: bool = True


@contextmanager
def quiet_events() -> Iterator[None]:
    """Silences the per-event 'Created'/'Deleted' lines; warnings and errors still print."""
    global _event_log
    was, _event_log = _event_log, False
    try:
        yield
    finally:
        _event_log = was


def event_url(event_id: Optional[str], calendar_id: Optional[str]) -> Optional[str]:
    """Rebuild an event's Google Calendar htmlLink from its stored id (eid = base64 of "<id> <cal>")."""
    if not event_id or not calendar_id:
        return None
    eid = base64.b64encode(f"{event_id} {calendar_id}".encode()).decode().rstrip("=")
    return f"https://www.google.com/calendar/event?eid={eid}"


class CalendarSyncer:
    """Synchronizes planned and adapted workouts to Google Calendar as all-day events."""

    def __init__(self) -> None:
        """Initializes Google API calendar service using configured service account."""
        self.scopes: List[str] = ['https://www.googleapis.com/auth/calendar']
        self.creds: service_account.Credentials = (
            service_account.Credentials.from_service_account_file(
                config.service_account_file, scopes=self.scopes
            )
        )
        self.service: Any = build('calendar', 'v3', credentials=self.creds)
        self.calendar_id: Optional[str] = config.google_calendar_id

    # Past-event adherence verdict -> title tag, shared rather than copied
    # (adherence.STATUS_LABELS). "Not yet" is in the map but unreachable here:
    # `mark_adherence_from_results` skips pending rows before rendering a verdict.
    _ADHERENCE_TAGS = STATUS_LABELS

    def sync_workout(
        self, workout: Workout, adherence: Optional[dict] = None
    ) -> Optional[str]:
        """Syncs a single workout to Google Calendar (creating or updating).

        Args:
            workout: The Workout details to synchronize.
            adherence: Optional backward-looking verdict for a *past* event,
                ``{"status": str, "actual": Optional[str], "reasons": [str]}``
                (built by `workout compare --mark`). When present, a status tag
                is prepended to the title and an "Adherence" header is prepended
                to the description.

        Returns:
            The Google Calendar event ID if sync was successful, or None.

        Raises:
            HttpError: If API call fails.
        """
        date_str = workout['date']
        sport_type = workout['sport_type']
        title = workout['title']
        description = workout['description']
        mod_reason = workout.get('modification_reason')
        google_event_id = workout.get('google_event_id')

        # Calculate end date (exclusive for all-day events: start_date + 1 day)
        start_date = datetime.strptime(date_str, "%Y-%m-%d")
        end_date = start_date + timedelta(days=1)
        end_date_str = end_date.strftime("%Y-%m-%d")

        # Format Summary and Description. The body is the session's CURRENT form only;
        # every earlier form is rendered by the history block below
        # (DESIGN_calendar_lineage.md §5).
        is_modified = bool(mod_reason)
        is_manual = workout.get('source') == 'manual'
        if workout.get('removed'):
            summary = f"[Deleted] {title}"
            event_description = description or ""
            removed_reason = workout.get('removed_reason')
            if removed_reason:
                event_description = f"{event_description}\n\nReason:\n{removed_reason}"
        elif is_modified:
            summary = f"[Adapted] {title}"
            event_description = f"{description or ''}\n\nReason:\n{mod_reason}"
        else:
            summary = title
            event_description = description or ""

        # Flag manually-added sessions so they're distinguishable at a glance from
        # coach-generated ones. The marker composes with any "[Adapted]" tag above
        # (a manual add can replace an existing session); a deleted event keeps the
        # "[Deleted]" framing alone since its removal is the salient state.
        if is_manual and not workout.get('removed'):
            summary = f"[Manual] {summary}"

        # Backward-looking adherence tag for a past event (Done/Missed/Partial/…).
        # Prepended so it reads first — for a finished session the verdict is the
        # salient state — and composes with any [Adapted]/[Manual] tag above.
        if adherence:
            tag = self._ADHERENCE_TAGS.get(adherence.get("status"))
            if tag:
                summary = f"[{tag}] {summary}"

        # Prepend the session's current load to the description if available.
        duration = workout.get('duration_minutes')
        tss = workout.get('tss')
        rpe = workout.get('rpe')
        prefix_parts = []
        if duration is not None:
            prefix_parts.append(f"Duration: {duration}m")
        if tss is not None:
            prefix_parts.append(f"TSS: {tss}")
        if rpe is not None:
            prefix_parts.append(f"RPE: {rpe}")
        prefix = " | ".join(prefix_parts)
        if prefix:
            if event_description:
                event_description = f"{prefix}\n\n{event_description}"
            else:
                event_description = prefix

        # The intensity target, rendered FROM the planned-zone columns here and never
        # stored, so the sentence cannot drift from the columns it describes
        # (DESIGN_intensity_distribution.md §9.8).
        target = intensity.format_planned_zones(workout)
        if target:
            event_description = (
                f"{event_description}\n\n{target}" if event_description else target
            )

        # Lifecycle footer, sitting just above the technical ID line so the two read as one
        # block directly under the current prescription — before the history, because it
        # describes this form of the session, not the earlier ones
        # (DESIGN_calendar_lineage.md §5). It carries when the session entered the plan
        # (`created_at`, always), and when/how often it has been eased (`adapted_at` /
        # `adaptation_count`, only once adapted). The load it was planned with is not
        # repeated here — the oldest history entry carries it, with its date and target.
        footer_lines: List[str] = []
        lifecycle_parts = []
        created_at = workout.get('created_at')
        adapted_at = workout.get('adapted_at')
        adaptation_count = workout.get('adaptation_count') or 0
        if created_at:
            lifecycle_parts.append(f"Planned: {fmt_timestamp(created_at)}")
        if adapted_at:
            lifecycle_parts.append(f"Last adapted: {fmt_timestamp(adapted_at)}")
        if adaptation_count:
            lifecycle_parts.append(f"Adapted ×{adaptation_count}")
        if lifecycle_parts:
            footer_lines.append(" · ".join(lifecycle_parts))

        # Append an identifier footer so each event stays traceable back to the plan
        # that produced it: goal/macro/meso are resolved from the workout's date, the
        # workout id comes from the row itself.
        id_parts = []
        ids = runtime.db.get_periodization_ids_for_date(date_str)
        if ids:
            objective_id, macrocycle_id, mesocycle_id = ids
            id_parts.append(f"Goal: {objective_id}")
            id_parts.append(f"Macro: {macrocycle_id}")
            id_parts.append(f"Meso: {mesocycle_id}")
        workout_id = workout.get('id')
        if workout_id is not None:
            id_parts.append(f"Workout: {workout_id}")
        if id_parts:
            footer_lines.append(" | ".join(id_parts))

        footer = "\n".join(footer_lines)

        # Prepend the adherence header so the verdict + actual effort sit at the top
        # of a past event's description, above the planned Duration/TSS and body.
        header = ""
        if adherence:
            status = adherence.get("status")
            tag = self._ADHERENCE_TAGS.get(status, status)
            header_lines = [f"Adherence: {tag}"]
            actual = adherence.get("actual")
            if actual:
                header_lines.append(f"Actual: {actual}")
            reasons = adherence.get("reasons") or []
            if reasons:
                header_lines.append(f"Notes: {', '.join(reasons)}")
            header = "\n".join(header_lines)

        # Every earlier form of this session, newest first — the event is the only place
        # the athlete can ask "what was this before?" without a terminal
        # (DESIGN_calendar_lineage.md §2). Placed last and sized last because it is the
        # part that yields: it takes the space the rest of the event does not need, so a
        # long prescription is never the thing that gets cut (§7).
        spare = calendar_lineage.MAX_DESCRIPTION - len(header) - len(event_description)
        history = calendar_lineage.for_workout(workout, budget=spare - len(footer) - 8)

        event_description = "\n\n".join(
            part for part in (header, event_description, footer, history) if part
        )

        event_body = {
            'summary': summary,
            'description': event_description,
            'start': {
                'date': date_str,
            },
            'end': {
                'date': end_date_str,
            },
            # Add metadata tag to identify TrainMate events
            'extendedProperties': {
                'private': {
                    'source': WORKOUT_EVENT_TAG,
                    'sport_type': sport_type
                }
            }
        }

        # If we have a saved google_event_id, try updating it
        if google_event_id:
            try:
                updated_event = self.service.events().update(
                    calendarId=self.calendar_id,
                    eventId=google_event_id,
                    body=event_body
                ).execute()

                # Record the push: store the event handle + the signature of what we
                # just pushed, so the row derives as `synced` until edited again.
                if workout.get('id') is not None:
                    runtime.db.mark_workout_pushed(
                        workout['id'], google_event_id, calendar_signature(workout)
                    )
                return google_event_id
            except HttpError as e:
                if e.resp.status in (404, 410):
                    print(
                        f"Warning: Calendar event {google_event_id} was deleted on Google "
                        f"Calendar. Re-creating a new one..."
                    )
                    # Fall through to insert new event
                else:
                    print(f"Error updating Google Calendar event: {e}")
                    raise e
            except Exception as e:
                print(f"Error updating Google Calendar event: {e}")
                raise e

        # Create a new event
        try:
            created_event = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event_body
            ).execute()
            new_event_id = created_event.get('id')
            if _event_log:
                print(
                    f"Created new calendar event for {fmt_date(date_str)} ({sport_type})."
                )
            
            # Record the push: store the new event handle + the signature of what we
            # just pushed, so the row derives as `synced` until edited again.
            if workout.get('id') is not None:
                runtime.db.mark_workout_pushed(
                    workout['id'], new_event_id, calendar_signature(workout)
                )
            return str(new_event_id)
        except Exception as e:
            print(f"Error inserting event to Google Calendar: {e}")
            raise e

    def sync_multiple(self, workouts: List[Workout]) -> List[Optional[str]]:
        """Syncs a list of workouts sequentially.

        Args:
            workouts: List of Workout objects to sync.

        Returns:
            A list of event IDs synced.
        """
        synced_ids = []
        for w in workouts:
            eid = self.sync_workout(w)
            synced_ids.append(eid)
        return synced_ids

    # ------------------------------------------------------------------
    # Inbound: external daily signals (alcohol, sleep, stress, …)
    # ------------------------------------------------------------------
    def sync_signals(self) -> int:
        """Pulls tagged signal events from the calendar into `daily_signals`.

        Incremental via `syncToken` (edit and delete detection come for free); falls
        back to a full pull of *all* tagged events on first run or when the token has
        expired (HTTP 410). The server-side `privateExtendedProperty` filter applies to
        the full pull only — the API forbids it alongside `syncToken`, so the incremental
        stream carries every changed event and is filtered client-side. Returns the number
        of rows upserted or deleted (DESIGN_calendar_signal_ingest.md §3, §6).
        """
        if not self.calendar_id:
            return 0

        state = runtime.db.get_sync_state(key="calendar_signals")
        use_token: Optional[str] = state.get("sync_token") if state else None

        changed = 0
        page_token: Optional[str] = None
        next_sync_token: Optional[str] = None
        while True:
            params: dict = {
                'calendarId': self.calendar_id,
                'showDeleted': True,
                'singleEvents': True,
                'maxResults': CALENDAR_SYNC_PAGE_SIZE,
            }
            # syncToken and the initial-sync params are mutually exclusive beyond
            # pageToken; reuse the *same* base params so the token stays valid.
            if use_token:
                params['syncToken'] = use_token
            else:
                params['privateExtendedProperty'] = f"source={config.calendar_signal_tag}"

            if page_token:
                params['pageToken'] = page_token
            try:
                resp = self.service.events().list(**params).execute()
            except HttpError as e:
                if e.resp.status == 410 and use_token:
                    # Expired token: discard it and restart with a full pull.
                    use_token = None
                    page_token = None
                    changed = 0
                    continue
                raise
            for event in resp.get('items', []):
                changed += self._ingest_signal_event(event)
            page_token = resp.get('nextPageToken')
            if not page_token:
                next_sync_token = resp.get('nextSyncToken')
                break

        runtime.db.set_sync_state(
            through_date=None,
            last_pull_utc=datetime.now(timezone.utc).isoformat(),
            key="calendar_signals",
            sync_token=next_sync_token,
        )
        return changed

    def _ingest_signal_event(self, event: dict) -> int:
        """Reconciles a single signal event into `daily_signals`. A cancelled event
        deletes its row; otherwise the row is upserted by event id. Returns 1 if the DB
        changed, else 0."""
        event_id = event.get('id')
        if not event_id:
            return 0
        if event.get('status') == 'cancelled':
            # Cancelled events arrive stripped of extendedProperties, so the tag guard
            # below can't run: a deleted row is the proof it was ours (§6).
            return 1 if runtime.db.delete_daily_signal_by_event(event_id) else 0

        private = (event.get('extendedProperties', {}) or {}).get('private', {}) or {}
        # On the incremental path this is the *only* filter (no server-side one is
        # allowed with a syncToken) — skip anything not actually ours (§3).
        if private.get('source') != config.calendar_signal_tag:
            return 0

        start = event.get('start', {}) or {}
        date = start.get('date') or (start.get('dateTime') or "")[:10]
        if not date:
            return 0

        runtime.db.upsert_daily_signal_by_event(
            google_event_id=event_id,
            date=date,
            metric=private.get('metric') or 'signal',
            value=self._parse_float(private.get('value')),
            text=self._signal_text(event),
            updated=event.get('updated'),
        )
        return 1

    @staticmethod
    def _parse_float(raw: Any) -> Optional[float]:
        """Best-effort parse of the optional numeric `value` tag; None when absent/bad."""
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _signal_text(event: dict) -> Optional[str]:
        """The human blurb handed to the coach: summary then description, trimmed."""
        parts = [event.get('summary'), event.get('description')]
        text = "\n".join(p.strip() for p in parts if p and p.strip())
        return text or None

    def add_signal_event(
        self, date: str, metric: str, value: Optional[float],
        text: Optional[str], existing_event_id: Optional[str] = None
    ) -> Optional[str]:
        """Authors (or updates) a tagged signal event for a single day.

        TrainMate becomes the first-party producer of the same `source=<signal_tag>`
        events the external syncer writes (DESIGN_signal_authoring.md). All-day, end
        exclusive = start + 1 day, matching workouts. When `existing_event_id` is given
        the event is updated in place (the idempotent upsert-by-(date,metric) path);
        otherwise a new one is inserted. Returns the event id, or None if no calendar
        is configured.
        """
        if not self.calendar_id:
            return None

        end_date_str = (
            datetime.strptime(date, "%Y-%m-%d").date() + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        private = {'source': config.calendar_signal_tag, 'metric': metric}
        if value is not None:
            private['value'] = str(value)
        event_body = {
            'summary': text or metric,
            'start': {'date': date},
            'end': {'date': end_date_str},
            'extendedProperties': {'private': private},
        }

        if existing_event_id:
            updated = self.service.events().update(
                calendarId=self.calendar_id, eventId=existing_event_id, body=event_body
            ).execute()
            return updated.get('id')
        created = self.service.events().insert(
            calendarId=self.calendar_id, body=event_body
        ).execute()
        return created.get('id')

    def list_workout_events(self) -> List[dict]:
        """Every workout event on the calendar, found by tag rather than by stored id.

        The tag is the only ownership handle that survives the database: a fresh DB
        (or a wipe that skipped the calendar) orphans events no row points at any
        more, so `workout prune-calendar` has to sweep from the calendar side.
        """
        if not self.calendar_id:
            return []

        events: List[dict] = []
        page_token: Optional[str] = None
        while True:
            params: dict = {
                'calendarId': self.calendar_id,
                'singleEvents': True,
                'maxResults': CALENDAR_SYNC_PAGE_SIZE,
                'privateExtendedProperty': f"source={WORKOUT_EVENT_TAG}",
            }
            if page_token:
                params['pageToken'] = page_token
            resp = self.service.events().list(**params).execute()
            events.extend(resp.get('items', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                return events

    def delete_event(self, google_event_id: str) -> bool:
        """Deletes an event from Google Calendar by id (source-agnostic).

        Args:
            google_event_id: The event ID to delete.

        Returns:
            True if the event was deleted; False if the API refused (best-effort —
            a failure is warned about, never raised, so teardown paths continue).
        """
        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=google_event_id
            ).execute()
            if _event_log:
                print(f"Deleted Google Calendar event {google_event_id}.")
            return True
        except Exception as e:
            print(f"Warning: Failed to delete Google Calendar event {google_event_id}: {e}")
            return False

    # Back-compat alias: workout teardown paths call this name.
    def delete_workout_event(self, google_event_id: str) -> bool:
        """Deletes a workout event from Google Calendar (see `delete_event`)."""
        return self.delete_event(google_event_id)

# Singleton instance
calendar_syncer = CalendarSyncer()


# Per-process memo: a single CLI command syncs signals at most once.
_signals_synced: bool = False


def sync_calendar_signals(force: bool = False) -> None:
    """Refreshes daily signals from the calendar, gating/throttling the actual sync.

    Rides along with `data pull` (force=True) and the auto-ensure-before-read path
    (force=False, where it runs at most once per process and skips entirely while the
    last sync is still fresh). Best-effort: no calendar configured is a silent no-op,
    and any Calendar error is swallowed with a warning so a data read never blocks
    (DESIGN_calendar_signal_ingest.md §6).
    """
    global _signals_synced
    if not config.google_calendar_id:
        return
    if not force:
        if _signals_synced:
            return
        # Skip if a recent sync already covers the freshness window (shared with the
        # Garmin metric-refresh cadence — "how fresh is fresh enough").
        state = runtime.db.get_sync_state(key="calendar_signals")
        if state and state.get("last_pull_utc"):
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(
                    state["last_pull_utc"]
                )
                if age <= timedelta(minutes=config.data_refresh_minutes):
                    step(
                        f"Calendar signals is fresh (last sync "
                        f"{int(age.total_seconds() // 60)}m ago); using cache. "
                        "Pass --force-pull to refresh now."
                    )
                    _signals_synced = True
                    return
            except (ValueError, TypeError):
                pass
    try:
        calendar_syncer.sync_signals()
        _signals_synced = True
    except Exception as e:
        warn(f"calendar signal sync skipped: {e}")
