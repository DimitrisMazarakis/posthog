import datetime
import json
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
)

from django.db import connection
from sentry_sdk.api import capture_message

from posthog.models import Person, SessionRecordingEvent, Team
from posthog.models.filters.sessions_filter import SessionsFilter
from posthog.models.session_recording_event import SessionRecordingViewed
from posthog.models.utils import namedtuplefetchall

DistinctId = str
Snapshots = List[Any]


OPERATORS = {"gt": ">", "lt": "<"}
SESSIONS_IN_RANGE_QUERY = """
    SELECT
        session_id,
        distinct_id,
        start_time,
        end_time,
        end_time - start_time as duration
    FROM (
        SELECT
            session_id,
            distinct_id,
            MIN(timestamp) as start_time,
            MAX(timestamp) as end_time,
            MAX(timestamp) - MIN(timestamp) as duration,
            COUNT(*) FILTER(where snapshot_data->>'type' = '2') as full_snapshots
        FROM posthog_sessionrecordingevent
        WHERE
            team_id = %(team_id)s
            AND timestamp >= %(start_time)s
            AND timestamp <= %(end_time)s
        GROUP BY distinct_id, session_id
    ) AS p
    WHERE full_snapshots > 0 {filter_query}
"""


class SessionRecording:
    def query_recording_snapshots(
        self, team: Team, session_id: str
    ) -> Tuple[Optional[DistinctId], Optional[datetime.datetime], Snapshots]:
        events = SessionRecordingEvent.objects.filter(team=team, session_id=session_id).order_by("timestamp")

        if len(events) == 0:
            return None, None, []

        return events[0].distinct_id, events[0].timestamp, [e.snapshot_data for e in events]

    def merge_snapshot_chunks(self, team: Team, session_id: str, unmerged_snapshots: List[Dict[str, any]]):
        snapshot_collectors: Dict[str, Dict[str, any]] = {}  # gather the chunks of a snapshot
        snapshots_and_collectors: List[Dict[str, any]] = []  # list the snapshots in order

        for chunk_or_snapshot in unmerged_snapshots:
            if chunk_or_snapshot.get("posthog_chunked"):  # it's a chunk
                collector = snapshot_collectors.get(chunk_or_snapshot["snapshot_id"])
                if not collector:
                    collector = {
                        "collector": True,
                        "event": chunk_or_snapshot,
                        "count": chunk_or_snapshot["chunk_count"],
                        "chunks": {},
                    }
                    snapshot_collectors[chunk_or_snapshot["snapshot_id"]] = collector
                    snapshots_and_collectors.append(collector)

                collector["chunks"][chunk_or_snapshot["chunk_index"]] = chunk_or_snapshot["chunk_data"]
            else:  # full snapshot
                snapshots_and_collectors.append({"snapshot": True, "data": chunk_or_snapshot})

        snapshots = []
        for snapshot_or_collector in snapshots_and_collectors:
            if snapshot_or_collector.get("collector"):
                has_all_chunks = True
                data = ""
                for i in range(snapshot_or_collector["count"]):
                    if not snapshot_or_collector["chunks"].get(i):
                        has_all_chunks = False
                        break
                    data = data + snapshot_or_collector["chunks"][i]

                if has_all_chunks:
                    snapshots.append(json.loads(data))
                else:
                    capture_message(
                        "Did not find all session recording chunks! Team: {}, Session: {}".format(team.pk, session_id)
                    )
            elif snapshot_or_collector.get("snapshot"):
                snapshots.append(snapshot_or_collector["data"])

        return snapshots

    def run(self, team: Team, session_recording_id: str, *args, **kwargs) -> Dict[str, Any]:
        from posthog.api.person import PersonSerializer

        distinct_id, start_time, unmerged_snapshots = self.query_recording_snapshots(team, session_recording_id)
        snapshots = self.merge_snapshot_chunks(team, session_recording_id, unmerged_snapshots)
        person = (
            PersonSerializer(Person.objects.get(team=team, persondistinctid__distinct_id=distinct_id)).data
            if distinct_id
            else None
        )

        return {
            "snapshots": snapshots,
            "person": person,
            "start_time": start_time,
        }


def query_sessions_in_range(
    team: Team, start_time: datetime.datetime, end_time: datetime.datetime, filter: SessionsFilter
) -> List[dict]:
    filter_query, filter_params = "", {}

    if filter.recording_duration_filter:
        filter_query = f"AND duration {OPERATORS[filter.recording_duration_filter.operator]} INTERVAL '%(min_recording_duration)s seconds'"
        filter_params = {
            "min_recording_duration": filter.recording_duration_filter.value,
        }

    with connection.cursor() as cursor:
        cursor.execute(
            SESSIONS_IN_RANGE_QUERY.format(filter_query=filter_query),
            {"team_id": team.id, "start_time": start_time, "end_time": end_time, **filter_params,},
        )

        results = namedtuplefetchall(cursor)

    return [row._asdict() for row in results]


# :TRICKY: This mutates sessions list
def filter_sessions_by_recordings(
    team: Team, sessions_results: List[Any], filter: SessionsFilter, query: Callable = query_sessions_in_range
) -> List[Any]:
    if len(sessions_results) == 0:
        return sessions_results

    min_ts = min(it["start_time"] for it in sessions_results)
    max_ts = max(it["end_time"] for it in sessions_results)

    session_recordings = query(team, min_ts, max_ts, filter)
    viewed_session_recordings = set(
        SessionRecordingViewed.objects.filter(team=team, user_id=filter.user_id).values_list("session_id", flat=True)
    )

    for session in sessions_results:
        session["session_recordings"] = list(
            collect_matching_recordings(session, session_recordings, filter, viewed_session_recordings)
        )

    if filter.limit_by_recordings:
        sessions_results = [session for session in sessions_results if len(session["session_recordings"]) > 0]
    return sessions_results


def collect_matching_recordings(
    session: Any, session_recordings: List[Any], filter: SessionsFilter, viewed: Set[str]
) -> Generator[Dict, None, None]:
    for recording in session_recordings:
        if matches(session, recording, filter, viewed):
            yield {"id": recording["session_id"], "viewed": recording["session_id"] in viewed}


def matches(session: Any, session_recording: Any, filter: SessionsFilter, viewed: Set[str]) -> bool:
    return (
        session["distinct_id"] == session_recording["distinct_id"]
        and session["start_time"] <= session_recording["end_time"]
        and session["end_time"] >= session_recording["start_time"]
        and (not filter.recording_unseen_filter or session_recording["session_id"] not in viewed)
    )
