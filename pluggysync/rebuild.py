"""`--rebuild`: layer 3 regenerated from layers 1 and 2, atomically.

Everything derived (events, ambiguities, document state, the nine projection
tables, the checkpoint) is cleared and replayed inside one BEGIN IMMEDIATE, so
a failure anywhere rolls back to the previous tables, indexes and views, all
still usable. Nothing is dropped or renamed: the views stay bound to the same
tables throughout, which is what makes the single transaction enough.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .events import rebuild_projection
from .ledger import transaction

CrashHook = Callable[..., None]


def rebuild(connection: sqlite3.Connection, crash_at: CrashHook | None = None) -> list[int]:
    """Returns the runs replayed, in order. An empty `runs` leaves no checkpoint row."""
    hook = crash_at or (lambda name, **context: None)
    with transaction(connection):
        hook("before_rebuild")
        replayed = rebuild_projection(connection, hook)
        hook("after_replay", runs=replayed)
    return replayed
