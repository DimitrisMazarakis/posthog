from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from dateutil.relativedelta import relativedelta
from django.utils.timezone import now

from ee.clickhouse.client import sync_execute
from ee.clickhouse.models.action import format_action_filter
from ee.clickhouse.models.property import parse_prop_clauses
from ee.clickhouse.sql.retention.retention import RETENTION_SQL
from posthog.constants import RETENTION_FIRST_TIME, TREND_FILTER_TYPE_ACTIONS, TREND_FILTER_TYPE_EVENTS
from posthog.models.action import Action
from posthog.models.entity import Entity
from posthog.models.filter import Filter
from posthog.models.team import Team
from posthog.queries.base import BaseQuery

PERIOD_TRUNC_HOUR = "toStartOfHour"
PERIOD_TRUNC_DAY = "toStartOfDay"
PERIOD_TRUNC_WEEK = "toStartOfWeek"
PERIOD_TRUNC_MONTH = "toStartOfMonth"


class ClickhouseRetention(BaseQuery):
    def calculate_retention(self, filter: Filter, team: Team, total_intervals: int) -> List[Dict[str, Any]]:

        period = filter.period or "Day"

        tdelta, trunc_func, t1 = self._determineTimedelta(total_intervals, period)

        filter._date_to = ((filter.date_to if filter.date_to else now()) + t1).isoformat()

        if period == "Hour":
            date_to = filter.date_to if filter.date_to else now()
            date_from = date_to - tdelta
        else:
            date_to = (filter.date_to if filter.date_to else now()).replace(hour=0, minute=0, second=0, microsecond=0)
            date_from = date_to - tdelta

        filter._date_from = date_from.isoformat()
        filter._date_to = date_to.isoformat()

        prop_filters, prop_filter_params = parse_prop_clauses(filter.properties, team)

        target_query, returning_query, target_params = self._get_event_filter(filter, trunc_func)

        result = sync_execute(
            RETENTION_SQL.format(
                target_query=target_query,
                returning_query=returning_query,
                filters="{filters}".format(filters=prop_filters) if filter.properties else "",
                trunc_func=trunc_func,
            ),
            {
                "team_id": team.pk,
                "start_date": date_from.strftime(
                    "%Y-%m-%d{}".format(" %H:%M:%S" if filter.period == "Hour" else " 00:00:00")
                ),
                "end_date": date_to.strftime(
                    "%Y-%m-%d{}".format(" %H:%M:%S" if filter.period == "Hour" else " 00:00:00")
                ),
                **prop_filter_params,
                **target_params,
                "period": period,
            },
        )

        result_dict = {}

        for res in result:
            result_dict.update({(res[0], res[1]): {"count": res[2], "people": []}})

        if period == "Week":
            date_from = date_from - timedelta(days=date_from.isoweekday() % 7)

        parsed = [
            {
                "values": [
                    result_dict.get((first_day, day), {"count": 0, "people": []})
                    for day in range(total_intervals - first_day)
                ],
                "label": "{} {}".format(period, first_day),
                "date": (date_from + self._determineTimedelta(first_day, period)[0]),
            }
            for first_day in range(total_intervals)
        ]

        return parsed

    def _get_event_filter(self, filter: Filter, trunc: str) -> Tuple[str, str, Dict]:
        query = ""
        params: Dict = {}

        first_time_retention = filter.retention_type == RETENTION_FIRST_TIME

        target_entity = (
            Entity({"id": "$pageview", "type": TREND_FILTER_TYPE_EVENTS})
            if not filter.target_entity
            else filter.target_entity
        )

        returning_entity = (
            (
                Entity({"id": "$pageview", "type": TREND_FILTER_TYPE_EVENTS})
                if not len(filter.entities) > 0
                else filter.entities[0]
            )
            if first_time_retention
            else target_entity
        )

        target_query, params = self._get_condition(target_entity)
        returning_query, returning_params = self._get_condition(
            returning_entity, "returning" if first_time_retention else ""
        )

        query = "AND {target_query}".format(target_query=target_query)
        returning_query = (
            "AND ({target_query} OR ({returning_query} AND {trunc_func}(toDateTime(%(start_date)s)) = {trunc_func}(timestamp)))".format(
                target_query=target_query, returning_query=returning_query, trunc_func=trunc
            )
            if first_time_retention
            else "AND {target_query}".format(target_query=target_query)
        )
        return query, returning_query, {**params, **returning_params}

    def _get_condition(self, target_entity: Entity, prepend: str = "") -> Tuple[str, Dict]:
        if target_entity.type == TREND_FILTER_TYPE_ACTIONS:
            action = Action.objects.get(pk=target_entity.id)
            action_query, params = format_action_filter(action, prepend=prepend, use_loop=True)
            condition = "e.uuid IN ({})".format(action_query)
        elif target_entity.type == TREND_FILTER_TYPE_EVENTS:
            condition = "e.event = %({}_event)s".format(prepend)
            params = {"{}_event".format(prepend): target_entity.id}
        else:
            condition = "e.event = %({}_event)s".format(prepend)
            params = {"{}_event".format(prepend): "$pageview"}
        return condition, params

    def _determineTimedelta(
        self, total_intervals: int, period: str
    ) -> Tuple[Union[timedelta, relativedelta], str, Union[timedelta, relativedelta]]:
        if period == "Hour":
            return timedelta(hours=total_intervals), PERIOD_TRUNC_HOUR, timedelta(hours=1)
        elif period == "Week":
            return timedelta(weeks=total_intervals), PERIOD_TRUNC_WEEK, timedelta(weeks=1)
        elif period == "Day":
            return timedelta(days=total_intervals), PERIOD_TRUNC_DAY, timedelta(days=1)
        elif period == "Month":
            return relativedelta(months=total_intervals), PERIOD_TRUNC_MONTH, relativedelta(months=1)
        else:
            raise ValueError(f"Period {period} is unsupported.")

    def run(self, filter: Filter, team: Team, *args, **kwargs) -> List[Dict[str, Any]]:
        total_intervals = kwargs.get("total_intervals", 11)
        return self.calculate_retention(filter, team, total_intervals)
