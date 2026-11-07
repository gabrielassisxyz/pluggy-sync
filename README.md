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
Parameters are `:account_id`, `:investment_id`, `:transaction_id`, `:day`, and for the
yield `:d1` and `:d2`.

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

### The yield of each caixinha between two days

Mercado Pago exposes each reserve's balance and rate, never its yield, and a
balance difference alone counts every deposit and withdrawal as yield. The
query takes two local days, `:d1 < :d2`, and an `:account_id`, and returns one
row per reserve with `prev_cents`, `curr_cents`, `transfers_cents`,
`yield_cents` and `reason`. `reason` is NULL when the yield is known.

How it is computed. `prev` and `curr` are the latest unambiguous account
observations by `d1` and by `d2` whose `reservedBalances` is an array (the
account walk's outcome does not matter: the array is self-contained inside the
sighted document, so a migrated run qualifies). The reserves compared are the
stable ids of both observations; a reserve only in `curr` starts at 0, one only
in `prev` is reported with reason `closed or missing at D2`, and an unstable
entry at either cutoff is listed on its own with reason `unstable`. Transfers
are the `TRANSFERENCIA_SALDO_RESERVADO` transactions on the account, in their
latest unambiguous version known by `d2`, with `date` after `prev`'s
`updatedAt` and up to `curr`'s. The window is bounded by the provider's
timestamp, not by the collection day: a transfer minutes before the snapshot
is already in the balance, and bounding by day would count it twice. Deposits
are negative on the checking account and withdrawals positive, so
`yield_cents = (curr - prev) + sum(transfers)`.

A transfer is tied to a reserve by the name after `Dinheiro reservado ` (money
in, `amount < 0`) or `Dinheiro retirado ` (money out, `amount > 0`), and only
when that name identifies exactly one stable reserve id in every account
observation from `prev` through `curr`: no other entry carries that name and
that id carries no other name. Everything that breaks the arithmetic is an
explicit reason on the reserve the transfer names, or on its own row when it
names none:

| reason | meaning |
| --- | --- |
| `coverage gap` | the account's transactions walk in `curr`'s run was not complete and repeat-free, or did not reach back to `prev`'s `updatedAt`; every row carries it |
| `closed or missing at D2` | the reserve was in `prev` and is not in `curr` |
| `unstable` | the entry's id is empty or duplicated in that observation |
| `currency`, `amount` | a value that is not `BRL`, not a finite number below 1e12, or not a two-decimal amount |
| `transfer removed` | the transfer was removed or orphaned on or before `d2` |
| `transfer pending` | the transfer is not `POSTED` |
| `transfer unresolved` | the transfer sits in a pending/posted pair |
| `transfer sign` | the description's prefix disagrees with the amount's sign |
| `unmatched transfer` | the name matches no stable reserve in the span |
| `name reused` | the name belongs to two reserve ids, or to an unstable entry, somewhere in the span |
| `name changed` | the reserve carried another name somewhere in the span |

Cents are `CAST(round(x * 100) AS INTEGER)` after validation. No row at all
means a cutoff has no qualifying observation, or `prev`'s timestamp is not
before `curr`'s.

<!-- query: caixinha_yield -->
```sql
WITH
cutoffs AS (
  SELECT a.observation_id, a.day, a.run_id, a.provider_updated_at
  FROM account_balance_history a
  JOIN daily_sightings s ON s.observation_id = a.observation_id
  JOIN versions v ON v.sha256 = s.sha256
  WHERE a.account_id = :account_id AND s.ambiguous = 0
    AND json_type(v.body, '$.bankData.reservedBalances') = 'array'
),
prev AS (SELECT * FROM cutoffs WHERE day <= :d1 ORDER BY day DESC LIMIT 1),
curr AS (SELECT * FROM cutoffs WHERE day <= :d2 ORDER BY day DESC LIMIT 1),
span AS (
  SELECT p.observation_id AS prev_obs, c.observation_id AS curr_obs, c.run_id AS curr_run,
         p.provider_updated_at AS start_at, c.provider_updated_at AS end_at,
         p.day AS prev_day, c.day AS curr_day
  FROM prev p, curr c
  WHERE datetime(p.provider_updated_at) IS NOT NULL
    AND datetime(c.provider_updated_at) IS NOT NULL
    AND datetime(p.provider_updated_at) < datetime(c.provider_updated_at)
),
span_reserves AS (
  SELECT r.*
  FROM reserved_balance_history r
  JOIN cutoffs o ON o.observation_id = r.observation_id, span
  WHERE o.day BETWEEN span.prev_day AND span.curr_day
),
name_ids AS (
  SELECT reserve_name, count(DISTINCT reserve_id) AS ids, min(reserve_id) AS one_id,
         max(CASE WHEN stable_identity = 0 THEN 1 ELSE 0 END) AS unstable_named
  FROM span_reserves WHERE reserve_name IS NOT NULL GROUP BY reserve_name
),
id_names AS (
  SELECT reserve_id, count(DISTINCT reserve_name) AS names
  FROM span_reserves WHERE stable_identity = 1 GROUP BY reserve_id
),
account_reason AS (
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM scopes s, span
    WHERE s.run_id = span.curr_run AND s.kind = 'transactions' AND s.parent = :account_id
      AND s.outcome = 'complete' AND s.infer_absence = 1 AND s.min_instant IS NOT NULL
      AND datetime(s.min_instant) <= datetime(span.start_at)
  ) THEN 'coverage gap' END AS reason
),
latest_tx AS (
  SELECT o.document_id, o.run_id, d.provider_id, v.body
  FROM observations o
  JOIN documents d ON d.id = o.document_id AND d.kind = 'transactions'
  JOIN versions v ON v.sha256 = o.sha256
  WHERE o.parent = :account_id AND o.observed_day <= :d2
    AND o.id = (SELECT max(x.id) FROM observations x
                WHERE x.document_id = o.document_id AND x.observed_day <= :d2
                  AND NOT EXISTS (SELECT 1 FROM ambiguities am
                                  WHERE am.run_id = x.run_id AND am.document_id = x.document_id))
),
transfers AS (
  SELECT t.provider_id, t.document_id,
         json_extract(t.body, '$.date') AS date,
         json_extract(t.body, '$.amount') AS amount,
         json_extract(t.body, '$.currencyCode') AS currency,
         json_extract(t.body, '$.description') AS description,
         json_extract(t.body, '$.status') AS status,
         EXISTS (SELECT 1 FROM events e
                 WHERE e.document_id = t.document_id AND e.run_id > t.run_id
                   AND e.event IN ('removed', 'parent_gone') AND e.effective_day <= :d2) AS gone
  FROM latest_tx t, span
  WHERE json_extract(t.body, '$.operationType') = 'TRANSFERENCIA_SALDO_RESERVADO'
    AND datetime(json_extract(t.body, '$.date')) > datetime(span.start_at)
    AND datetime(json_extract(t.body, '$.date')) <= datetime(span.end_at)
),
judged AS (
  SELECT tr.*,
    CASE WHEN description LIKE 'Dinheiro reservado %' THEN substr(description, 20)
         WHEN description LIKE 'Dinheiro retirado %' THEN substr(description, 19) END AS reserve_name,
    CASE
      WHEN gone THEN 'transfer removed'
      WHEN status IS NOT 'POSTED' THEN 'transfer pending'
      WHEN provider_id IN (SELECT pending_id FROM pending_posted_candidates
                           UNION SELECT posted_id FROM pending_posted_candidates)
        THEN 'transfer unresolved'
      WHEN description NOT LIKE 'Dinheiro reservado %' AND description NOT LIKE 'Dinheiro retirado %'
        THEN 'unmatched transfer'
      WHEN (description LIKE 'Dinheiro reservado %' AND NOT amount < 0)
        OR (description LIKE 'Dinheiro retirado %' AND NOT amount > 0) THEN 'transfer sign'
      WHEN currency IS NOT 'BRL' THEN 'currency'
      WHEN typeof(amount) NOT IN ('integer', 'real') OR abs(amount) >= 1e12
        OR abs(amount * 100 - round(amount * 100)) > 1e-7 THEN 'amount'
    END AS transfer_reason
  FROM transfers tr
),
resolved AS (
  SELECT j.provider_id, j.date, j.reserve_name,
    CASE WHEN n.ids = 1 AND n.unstable_named = 0 THEN n.one_id END AS reserve_id,
    CASE
      WHEN j.transfer_reason IS NOT NULL THEN j.transfer_reason
      WHEN n.reserve_name IS NULL THEN 'unmatched transfer'
      WHEN n.ids > 1 OR n.unstable_named = 1 THEN 'name reused'
      WHEN i.names > 1 THEN 'name changed'
    END AS reason,
    CAST(round(j.amount * 100) AS INTEGER) AS cents
  FROM judged j
  LEFT JOIN name_ids n ON n.reserve_name = j.reserve_name
  LEFT JOIN id_names i ON i.reserve_id = n.one_id
),
prev_entries AS (SELECT r.* FROM reserved_balance_history r, span WHERE r.observation_id = span.prev_obs),
curr_entries AS (SELECT r.* FROM reserved_balance_history r, span WHERE r.observation_id = span.curr_obs),
stable AS (
  SELECT reserve_id FROM prev_entries WHERE stable_identity = 1
  UNION SELECT reserve_id FROM curr_entries WHERE stable_identity = 1
),
per_reserve AS (
  SELECT s.reserve_id,
    coalesce(c.reserve_name, p.reserve_name) AS reserve_name,
    p.reserve_id IS NOT NULL AS in_prev, c.reserve_id IS NOT NULL AS in_curr,
    EXISTS (SELECT 1 FROM prev_entries u WHERE u.reserve_id = s.reserve_id AND u.stable_identity = 0)
      OR EXISTS (SELECT 1 FROM curr_entries u WHERE u.reserve_id = s.reserve_id AND u.stable_identity = 0)
      AS unstable_somewhere,
    p.amount AS prev_amount, c.amount AS curr_amount,
    p.amount_status AS prev_status, c.amount_status AS curr_status,
    p.currency AS prev_currency, c.currency AS curr_currency,
    (SELECT sum(cents) FROM resolved r WHERE r.reserve_id = s.reserve_id AND r.reason IS NULL)
      AS transfers_cents,
    (SELECT reason FROM resolved r WHERE r.reserve_id = s.reserve_id AND r.reason IS NOT NULL
     ORDER BY date LIMIT 1) AS transfer_reason
  FROM stable s
  LEFT JOIN prev_entries p ON p.reserve_id = s.reserve_id AND p.stable_identity = 1
  LEFT JOIN curr_entries c ON c.reserve_id = s.reserve_id AND c.stable_identity = 1
),
decided AS (
  SELECT reserve_id, reserve_name,
    CASE WHEN in_prev THEN CAST(round(prev_amount * 100) AS INTEGER) END AS prev_cents,
    CASE WHEN in_curr THEN CAST(round(curr_amount * 100) AS INTEGER) END AS curr_cents,
    transfers_cents,
    CASE
      WHEN (SELECT reason FROM account_reason) IS NOT NULL THEN (SELECT reason FROM account_reason)
      WHEN unstable_somewhere THEN 'unstable'
      WHEN NOT in_curr THEN 'closed or missing at D2'
      WHEN (in_prev AND prev_currency IS NOT 'BRL') OR curr_currency IS NOT 'BRL' THEN 'currency'
      WHEN (in_prev AND (prev_status <> 'ok' OR typeof(prev_amount) NOT IN ('integer', 'real')
                         OR abs(prev_amount) >= 1e12
                         OR abs(prev_amount * 100 - round(prev_amount * 100)) > 1e-7))
        OR curr_status <> 'ok' OR typeof(curr_amount) NOT IN ('integer', 'real')
        OR abs(curr_amount) >= 1e12
        OR abs(curr_amount * 100 - round(curr_amount * 100)) > 1e-7 THEN 'amount'
      ELSE transfer_reason
    END AS reason
  FROM per_reserve
)
SELECT reserve_id, reserve_name, prev_cents, curr_cents, transfers_cents,
       CASE WHEN reason IS NULL
            THEN (curr_cents - coalesce(prev_cents, 0)) + coalesce(transfers_cents, 0) END AS yield_cents,
       reason
FROM decided
UNION ALL
SELECT reserve_id, reserve_name, prev_cents, curr_cents, NULL, NULL, 'unstable'
FROM (
  SELECT reserve_id, reserve_name, CAST(round(amount * 100) AS INTEGER) AS prev_cents,
         NULL AS curr_cents FROM prev_entries WHERE stable_identity = 0
  UNION ALL
  SELECT reserve_id, reserve_name, NULL, CAST(round(amount * 100) AS INTEGER)
  FROM curr_entries WHERE stable_identity = 0
)
UNION ALL
SELECT NULL, reserve_name, NULL, NULL, cents, NULL, reason
FROM resolved WHERE reserve_id IS NULL
ORDER BY reserve_id, reserve_name
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
