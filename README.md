# pluggy-sync

Pulls everything a Pluggy account will hand over into one local SQLite file, and
keeps every version of everything it ever received.

No dependencies beyond the Python standard library, no install step, no build.
One entry point, one database.

## Running it

Three environment variables, the same three Pluggy itself documents:

```
PLUGGY_CLIENT_ID       from the Pluggy dashboard
PLUGGY_CLIENT_SECRET   from the Pluggy dashboard
PLUGGY_ITEM_IDS        connection ids, comma-separated
```

```sh
./pluggy_sync.py                          # the legacy sync, into pluggy.db (or PLUGGY_DB)
./pluggy_sync.py --candidate PATH         # the ledger sync, into a dedicated candidate file
./pluggy_sync.py --rebuild --candidate PATH   # regenerate the derived layer of a candidate
```

Until the cutover, the plain invocation is the legacy collector: one row per
record, overwritten on every run. The ledger described below runs only against
a **candidate** database named with `--candidate`. A candidate is never the
live file nor an alias of it, and never an unignored path inside this
repository, because the file holds a person's complete financial history. The
historical import, the integrity check and the promotion of a candidate to the
live file are the next steps and are not here yet.

Re-running is safe. The ledger never overwrites: a run adds what it saw.

## What it stores

The database has three layers.

**Layer 1, the archive.** Every HTTP attempt, retries and failures included, is a
row in `responses`: method, URL, status, response headers, the exact bytes
received (zlib-compressed, with their SHA-256 and length), and the instant it
arrived. The row is committed the moment the bytes land, before anything parses
them. The API key travels in a request header and request headers are never
stored; the authentication call is never archived. One full run of four
connections archives about 1.8 MB of JSON, 150 KB compressed.

**Layer 2, the sightings.** `documents` is one row per `(kind, provider id)`,
`versions` one row per distinct document body (canonical JSON, keyed by its
SHA-256), and `observations` one row per sighting of a version in a run, with
the response and position it came from, the parent it was fetched under, and
the local day it was observed. A document seen unchanged on 365 days has 365
observations and one version. `runs` and `scopes` record what each run set out
to fetch and what each walk came back with.

Layers 1 and 2 are append-only by trigger: an `UPDATE` or `DELETE` on them
aborts inside the database, whatever issued it.

**Layer 3, derived.** `events` (what changed, when), `ambiguities`,
`document_state` and the nine current-state tables. Everything here is a
function of the first two layers, and `--rebuild` regenerates it from scratch
inside one transaction.

### The current-state tables

`items`, `accounts`, `transactions`, `bills`, `investments`,
`investment_transactions`, `consents`, `identities` and `categories` keep their
names, their extracted columns and their indexes, so queries written against
the legacy collector keep working. Each row is the latest sighting of a
document. `raw` is the canonical JSON of that sighting, re-serialised with
sorted keys; the provider's bytes are in `responses`. `fetched_at` is the
instant of the sighting. Parent columns (`transactions.account_id`,
`bills.account_id`, `investment_transactions.investment_id`, `item_id`) come
from the walk the document was fetched in, never from the body: bills and
investment movements carry no parent in their JSON.

Four columns are new:

| column | meaning |
| --- | --- |
| `first_seen_run` | the run that first sighted the document; never overwritten |
| `last_seen_run` | the run that last sighted it |
| `state` | `present`, `removed` (an absence-safe walk no longer returned it) or `parent_gone` (its account or investment was removed) |
| `ambiguous_run` | set when the last run saw the document with two bodies or under two parents; NULL when clean |

### Events

Computed once per run after every walk is derived, so they do not depend on
walk order. `appeared`, `changed`, `moved` (a new parent), `reappeared` (seen
again after `removed`), `removed` and `parent_gone`, with `old_*`/`new_*` body
hashes and parents and the instant and local day the change is effective.

Absence is an inference and is drawn only where the evidence allows it. A walk
may infer absence when it was complete with no repeated id across pages
(`scopes.infer_absence = 1`), and only over the range it actually returned: for
dated kinds (`transactions`, `investment_transactions`, `bills`) a document is
a candidate for `removed` only when its business date is on or after the
earliest date the walk returned (`scopes.min_date`). Older documents are simply
not covered and stay `present`. A document that is ambiguous is removed only
when every candidate parent was walked absence-safe in one run.

### Coverage

`scope_coverage(kind, parent, last_complete_run, last_complete_at,
last_absence_safe_run, last_min_date, last_attempt_run, last_outcome)` says,
per walk, what the last absence-safe run covered. `document_coverage` applies it
per document: `covered = 1` when the document's walk was absence-safe in the
projected run and, for a dated kind, its business date is inside that walk's
range. A row that is `present`, unambiguous and covered is as good as the
provider's answer today; a row that is present and not covered is "last
observed, not covered": nobody has proven it gone, nobody has seen it lately.

## Views

Ids exposed are the provider's; `day` is the local day (`America/Sao_Paulo`)
the sighting was observed, stored at ingest.

| view | one row per |
| --- | --- |
| `run_coverage` | run: outcome, API calls, and the owed walks split by outcome |
| `scope_coverage` | `(kind, parent)`: the last complete and last absence-safe walks and the latter's range |
| `document_coverage` | document: state, business date and whether it is covered today |
| `account_balance_history` | account per day: balance, closing balance, invested balance, card limits, the provider's `updatedAt` |
| `reserved_balance_history` | reserve sub-account entry (a Mercado Pago *caixinha*) per account observation: amount, indexer, CDI percentage, and whether its id is stable |
| `investment_history` | investment per day: original, gross and net amounts, the two tax fields, rate, due date, status |
| `transaction_versions` | distinct version of a transaction: a row whenever the body, the parent or the ambiguity changed, or an event happened |
| `pending_posted_candidates` | pending transaction beside a posted one that looks like the same purchase |

`investment_history.taxes` reads as provisioned income tax and `taxes2` as
provisioned IOF (non-zero only on a CDB bought inside the 30-day IOF window).
That is inferred from the values; the provider publishes no contract for either
field.

In `reserved_balance_history`, `amount`, `currency`, `indexer`,
`indexer_ratio` and `cdi_percentage` are read only when the entry has exactly
one `availableAmounts` element; otherwise they are NULL and `amount_status`
says why. `stable_identity` is 1 only when the entry's `identification` is a
non-empty string unique within that account observation; an unstable entry is
located only inside its own observation and is never joined across
observations.

Monetary values come out of SQLite's JSON functions as floating point. Validate
before summing: finite, below 1e12 in absolute value, at most two decimals after
rounding to 1e-9; then `CAST(round(x * 100) AS INTEGER)` and add cents.
`amountOriginal` on an investment can carry sub-cent digits after a partial
withdrawal and is never summed.

## Example queries

Every query below is executed by the test suite against a synthetic ledger.
Parameters are `:account_id`, `:investment_id`, `:transaction_id`, `:day`.

### The balance of an account as of a day, with its coverage

`no_run` means no run spanned that day; `covered` means the account's walk
closed absence-safe on that day; `not covered` means a run happened but that
walk did not vouch for the account.

<!-- query: balance_as_of -->
```sql
WITH latest AS (
  SELECT * FROM account_balance_history
  WHERE account_id = :account_id AND day <= :day
  ORDER BY day DESC LIMIT 1
)
SELECT l.day, l.observed_at, l.provider_updated_at, l.currency, l.balance,
       l.closing_balance, l.credit_limit, l.available_credit_limit,
       CASE
         WHEN NOT EXISTS (SELECT 1 FROM runs
                          WHERE :day BETWEEN started_day
                                         AND coalesce(finished_day, started_day))
           THEN 'no_run'
         WHEN EXISTS (SELECT 1 FROM scopes s
                      JOIN document_state ds ON ds.parent = s.parent
                      JOIN documents d ON d.id = ds.document_id
                      WHERE d.kind = 'accounts' AND d.provider_id = :account_id
                        AND s.kind = 'accounts' AND s.closed_day = :day
                        AND s.infer_absence = 1)
           THEN 'covered'
         ELSE 'not covered'
       END AS coverage
FROM latest l
```

### The reserves of an account as of a day

Select the account observation first and join the reserves onto it: an
observation whose array is empty has no reserve rows, and the join is what
keeps it from being skipped. A reserve absent from that observation has no
balance on that day and is never carried forward from an older one.

<!-- query: reserves_as_of -->
```sql
WITH latest AS (
  SELECT observation_id, day, provider_updated_at FROM account_balance_history
  WHERE account_id = :account_id AND day <= :day
  ORDER BY day DESC LIMIT 1
)
SELECT l.day, l.provider_updated_at, r.position, r.reserve_id, r.reserve_name,
       r.stable_identity, r.currency, r.amount, r.cdi_percentage, r.amount_status
FROM latest l
LEFT JOIN reserved_balance_history r ON r.observation_id = l.observation_id
ORDER BY r.position
```

### The history of a CDB

<!-- query: cdb_history -->
```sql
SELECT day, provider_updated_at, provider_date, currency, amount_original, amount_gross,
       taxes, taxes2, amount_net, rate, rate_type, due_date, status
FROM investment_history
WHERE investment_id = :investment_id
ORDER BY day
```

### Everything a transaction went through

<!-- query: transaction_history -->
```sql
SELECT run_id, day, observed_at, account_id, status, date, amount, currency, description,
       category, bill_post_date, events, ambiguous
FROM transaction_versions
WHERE provider_id = :transaction_id
ORDER BY run_id, observed_at
```

### The clean list

Present, unambiguous, covered, and, for a pending transaction, not matched to a
posted twin. A pending/posted pair is counted once, through its posted member.

<!-- query: clean_list -->
```sql
SELECT t.id, t.account_id, t.date, t.amount, t.description
FROM transactions t
JOIN document_coverage c ON c.kind = 'transactions' AND c.provider_id = t.id
WHERE t.state = 'present' AND t.ambiguous_run IS NULL AND c.covered = 1
  AND NOT (json_extract(t.raw, '$.status') = 'PENDING'
           AND t.id IN (SELECT pending_id FROM pending_posted_candidates))
ORDER BY t.date DESC, t.id
```

### The unresolved list

Pending transactions the ledger cannot settle: either their date fell before
the range of the last absence-safe walk, so nobody can say whether they were
posted or dropped, or a posted twin exists and the provider gives no link.

<!-- query: unresolved_list -->
```sql
SELECT t.id, t.account_id, t.date, t.amount, t.description,
       CASE WHEN c.covered = 0 THEN 'pending, not covered' ELSE 'pending with a posted twin' END
         AS reason
FROM transactions t
JOIN document_coverage c ON c.kind = 'transactions' AND c.provider_id = t.id
WHERE t.state = 'present' AND t.ambiguous_run IS NULL
  AND json_extract(t.raw, '$.status') = 'PENDING'
  AND (c.covered = 0 OR t.id IN (SELECT pending_id FROM pending_posted_candidates))
ORDER BY t.date DESC, t.id
```

### Last observed, not covered

<!-- query: not_covered_list -->
```sql
SELECT t.id, t.account_id, t.date, t.amount, t.description, t.fetched_at AS last_observed_at
FROM transactions t
JOIN document_coverage c ON c.kind = 'transactions' AND c.provider_id = t.id
WHERE t.state = 'present' AND t.ambiguous_run IS NULL AND c.covered = 0
  AND json_extract(t.raw, '$.status') <> 'PENDING'
ORDER BY t.date DESC, t.id
```

### Pending and posted twins

<!-- query: pending_pairs -->
```sql
SELECT account_id, pending_id, pending_date, pending_covered, posted_id, posted_date,
       amount, currency, description
FROM pending_posted_candidates
ORDER BY posted_date DESC
```

## Locks, backups and file modes

- One writer per database file, across processes: `flock` on `<file>.lock`,
  exclusive for writers and shared for read-only modes, released by the kernel
  however the holder dies. A second writer exits at once instead of queueing.
  The legacy sync takes the same lock.
- A schema migration backs the file up first, as `<file>-pre-v<N>`, verified,
  published by exclusive create and never overwritten.
- Every file the program creates is mode `0600`: the database, its journal,
  the lock, backups and temporary files. The archive holds names, document
  numbers and counterparties.
- A run that crashes is closed on the next start, as `interrupted`, with
  exactly what it managed to archive; nothing is resumed and nothing is lost.

## What the API will not do

- **No listing of connections.** Pluggy publishes no endpoint that enumerates
  items, deliberately: a leaked key cannot discover what it can read. A
  connection whose id is not in `PLUGGY_ITEM_IDS` is invisible, and no amount of
  code fixes that.
- **No refresh on demand.** On the MeuPluggy connector, `PATCH /items/{id}` is
  refused outright. Freshness is whatever the provider's own daily sync last
  produced; `items.last_updated` and `items.next_auto_sync` record it.
- **Read only.** Nothing here can move money or change anything at a bank.

History reaches back at least fourteen months in practice. The provider does not
publish the retention window, so collect regularly rather than count on it.

## Scheduling

The provider syncs roughly every 24 hours and prohibits batch processes against
the API. Once a day, after `next_auto_sync` has passed, is the whole of it;
anything more frequent re-reads the same rows.

## Development

```sh
bin/install-hooks   # once after clone: secret scan on every commit
bin/ci              # the exact gate CI runs: gitleaks, shellcheck, ruff, tests
python3 -m unittest discover -s tests -t .
```

`bin/ci` needs `gitleaks`, `shellcheck` and `ruff` on the machine and says which one
is missing. Tests never touch the network or real data; fixtures are synthetic, and
the example queries in this file are run by the suite.

Work is tracked in `.beads/` with [br](https://github.com/Dicklesworthstone/beads_rust):
`br ready` lists what can be picked up next. `AGENTS.md` is the spec for anyone, human
or agent, changing the code.
