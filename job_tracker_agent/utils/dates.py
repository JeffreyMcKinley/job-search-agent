"""Date helpers shared by every command."""

from __future__ import annotations

from datetime import datetime, timezone


def today() -> str:
    """Today's date as YYYY-MM-DD in the local timezone.

    Timezone-aware so the date matches the user's wall clock: a UTC-based
    date would roll over early on evening runs in US timezones and stop
    matching the dated job folder names in ~/.proficiently/jobs/.
    """
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
