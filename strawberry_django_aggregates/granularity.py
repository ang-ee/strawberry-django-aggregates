"""Date / datetime bucketing granularities.

Two parallel tracks, mirroring Odoo's ``READ_GROUP_TIME_GRANULARITY`` and
``READ_GROUP_NUMBER_GRANULARITY`` (``odoo/models.py:217``):

- **TIME**: ``date_trunc`` — returns the truncated DateTime. For "files
  created per month."
- **NUMBER**: ``date_part::int`` — returns an Int. For "files created
  per day-of-week" (cohort/heatmap analytics).

Timezone correctness: callers passing a ``tz`` value get UTC-stored
timestamps cast first to UTC then to the user's tz, *then* truncated.
This is the same wrap order Odoo uses; truncating UTC directly
mis-buckets any timestamp near a date boundary in the user's tz.
"""

from __future__ import annotations

from enum import StrEnum


class TimeGranularity(StrEnum):
    """``date_trunc(granularity, ts)`` — returns DateTime."""

    YEAR    = "year"
    QUARTER = "quarter"
    MONTH   = "month"
    WEEK    = "week"
    DAY     = "day"
    HOUR    = "hour"
    MINUTE  = "minute"
    SECOND  = "second"


class NumberGranularity(StrEnum):
    """``date_part(part, ts)::int`` — returns Int.

    Enables cohort and heatmap analytics: signups per hour-of-day across
    all dates, files per day-of-week, etc. Odoo added these in 17.3
    (PR #159528) after years of community requests.
    """

    YEAR_NUMBER     = "year_number"
    QUARTER_NUMBER  = "quarter_number"
    MONTH_NUMBER    = "month_number"
    ISO_WEEK_NUMBER = "iso_week_number"
    DAY_OF_YEAR     = "day_of_year"
    DAY_OF_MONTH    = "day_of_month"
    DAY_OF_WEEK     = "day_of_week"
    HOUR_NUMBER     = "hour_number"
    MINUTE_NUMBER   = "minute_number"
    SECOND_NUMBER   = "second_number"


# Mapping NumberGranularity members to the Postgres date_part field name.
NUMBER_GRANULARITY_PART: dict[NumberGranularity, str] = {
    NumberGranularity.YEAR_NUMBER:     "year",
    NumberGranularity.QUARTER_NUMBER:  "quarter",
    NumberGranularity.MONTH_NUMBER:    "month",
    NumberGranularity.ISO_WEEK_NUMBER: "week",
    NumberGranularity.DAY_OF_YEAR:     "doy",
    NumberGranularity.DAY_OF_MONTH:    "day",
    NumberGranularity.DAY_OF_WEEK:     "dow",
    NumberGranularity.HOUR_NUMBER:     "hour",
    NumberGranularity.MINUTE_NUMBER:   "minute",
    NumberGranularity.SECOND_NUMBER:   "second",
}


Granularity = TimeGranularity | NumberGranularity
"""Either kind of granularity — used in spec inputs."""
