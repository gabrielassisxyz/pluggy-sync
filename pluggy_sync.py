#!/usr/bin/env python3
"""Pull everything Pluggy will hand over into a local SQLite file, and keep
every version of everything it ever received.

This file is the entry point; the code lives in the `pluggysync` package
beside it (standard library only, no install step). Without a flag it runs the
ledger sync against `pluggy.db` (or `PLUGGY_DB`); `--help` lists the other
modes. The legacy overwrite-only sync was retired at the cutover, and the
shape of the databases it wrote is kept in `pluggysync/legacy_schema.py` for
the import and the promotion that still read them.
"""

from __future__ import annotations

import sys

from pluggysync.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
