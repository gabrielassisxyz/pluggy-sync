"""The README's example queries, read from the README itself so the
documentation is what the suite executes."""

from __future__ import annotations

import pathlib
import re

README = pathlib.Path(__file__).resolve().parents[2] / "README.md"
_BLOCK = re.compile(r"<!-- query: (?P<name>[a-z_]+) -->\n```sql\n(?P<sql>.*?)\n```", re.S)


def readme_queries() -> dict[str, str]:
    return {match["name"]: match["sql"] for match in _BLOCK.finditer(README.read_text())}


def readme_query(name: str) -> str:
    queries = readme_queries()
    if name not in queries:
        raise KeyError(f"README has no query named {name!r}; it has {sorted(queries)}")
    return queries[name]
