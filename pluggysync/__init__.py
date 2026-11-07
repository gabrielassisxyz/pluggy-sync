"""The collector behind `pluggy_sync.py`, split by responsibility.

`transport` is the HTTP boundary: one function turns a request into bytes and
never raises. `walk` turns those bytes into pages and says whether a walk
finished. Standard library only: the package is imported from the checkout,
there is no install step.
"""
