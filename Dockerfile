# Pinned so a rebuild nobody asked for cannot change Python or SQLite
# under a live ledger. Measured clean with --check on a glibc-written ledger.
FROM python:3.14.7-alpine3.24 AS test

# git is test-only: the refusal tests need rev-parse and check-ignore,
# and the suite reads .gitignore. The final stage ships without it.
RUN apk add --no-cache git

# Owned by the uid that runs the suite, or git refuses the tree.
RUN mkdir -p /src && chown 1000:1000 /src
WORKDIR /src
COPY --chown=1000:1000 pluggy_sync.py ./
COPY --chown=1000:1000 pluggysync ./pluggysync
COPY --chown=1000:1000 tests ./tests
COPY --chown=1000:1000 README.md ./
COPY --chown=1000:1000 .gitignore ./
USER 1000:1000
RUN git init
RUN python -m unittest discover -s tests -t .

FROM python:3.14.7-alpine3.24 AS final
# Root-owned on purpose: with PLUGGY_DB unset the program would otherwise create
# its database inside the container and lose it on exit, reporting success.
WORKDIR /app
COPY pluggy_sync.py ./
COPY pluggysync ./pluggysync
ARG PLUGGY_CODE_VERSION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 PLUGGY_CODE_VERSION=${PLUGGY_CODE_VERSION}
USER 1000:1000
ENTRYPOINT ["python", "/app/pluggy_sync.py"]
