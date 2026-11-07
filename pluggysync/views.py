"""History and coverage views over the ledger. No stored data: every row here
is a query over observations, events, scopes, runs and the projections.

Ids exposed are `documents.provider_id`; `day` is `observations.observed_day`.
When a document is sighted more than once on one local day, the daily views
take the latest run of that day and, within it, the sighting with the highest
id, or the deterministic display sighting (lowest parent, lowest hash, highest
id) when the document is ambiguous in that run. `observed_at` is exposed as
evidence, never used for ordering: a wall clock can move backward.
"""

from __future__ import annotations

# Business date in SQL, per kind. `date()` converts an instant with any offset
# to its UTC calendar date; the midnight-UTC kinds take their date part as is.
BUSINESS_DATE_SQL = """CASE d.kind
    WHEN 'transactions' THEN date(json_extract(v.body, '$.date'))
    WHEN 'investment_transactions' THEN substr(json_extract(v.body, '$.date'), 1, 10)
    WHEN 'bills' THEN substr(json_extract(v.body, '$.dueDate'), 1, 10)
  END"""

AMBIGUOUS_SQL = (
    "EXISTS (SELECT 1 FROM ambiguities a"
    " WHERE a.run_id = o.run_id AND a.document_id = o.document_id)"
)

VIEWS: list[str] = [
    """CREATE VIEW run_coverage AS
SELECT
  r.id AS run_id, r.started_at, r.started_day, r.finished_at, r.finished_day, r.outcome,
  r.api_calls,
  (SELECT coalesce(sum(expected), 0) FROM scopes s WHERE s.run_id = r.id) AS expected,
  (SELECT count(*) FROM scopes s WHERE s.run_id = r.id AND s.expected = 1
     AND s.outcome = 'complete') AS complete,
  (SELECT count(*) FROM scopes s WHERE s.run_id = r.id AND s.expected = 1
     AND s.outcome = 'unavailable') AS unavailable,
  (SELECT count(*) FROM scopes s WHERE s.run_id = r.id AND s.expected = 1
     AND s.outcome = 'incomplete') AS incomplete,
  (SELECT count(*) FROM scopes s WHERE s.run_id = r.id AND s.expected = 1
     AND s.outcome = 'skipped') AS skipped,
  (SELECT count(*) FROM scopes s WHERE s.run_id = r.id AND s.expected = 1
     AND s.outcome = 'pending') AS pending,
  (SELECT count(*) FROM ambiguities a WHERE a.run_id = r.id) AS ambiguous_documents
FROM runs r""",
    # One sighting per document per local day: the latest run of the day, and
    # within it the highest id, or the display sighting if ambiguous there.
    f"""CREATE VIEW daily_sightings AS
SELECT o.document_id, o.run_id, o.observed_day AS day, o.id AS observation_id,
       o.parent, o.sha256, o.observed_at, {AMBIGUOUS_SQL} AS ambiguous
FROM observations o
WHERE o.run_id = (SELECT max(x.run_id) FROM observations x
                  WHERE x.document_id = o.document_id AND x.observed_day = o.observed_day)
  AND o.id = CASE WHEN {AMBIGUOUS_SQL}
    THEN (SELECT y.id FROM observations y
          WHERE y.document_id = o.document_id AND y.run_id = o.run_id
            AND y.observed_day = o.observed_day
          ORDER BY y.parent, y.sha256, y.id DESC LIMIT 1)
    ELSE (SELECT max(y.id) FROM observations y
          WHERE y.document_id = o.document_id AND y.run_id = o.run_id
            AND y.observed_day = o.observed_day)
  END""",
    """CREATE VIEW account_balance_history AS
SELECT s.run_id, s.day, s.observed_at, json_extract(v.body, '$.updatedAt') AS provider_updated_at,
       d.provider_id AS account_id, s.observation_id,
       json_extract(v.body, '$.name') AS name,
       json_extract(v.body, '$.type') AS type,
       json_extract(v.body, '$.currencyCode') AS currency,
       json_extract(v.body, '$.balance') AS balance,
       json_extract(v.body, '$.bankData.closingBalance') AS closing_balance,
       json_extract(v.body, '$.bankData.automaticallyInvestedBalance')
         AS automatically_invested_balance,
       json_extract(v.body, '$.creditData.creditLimit') AS credit_limit,
       json_extract(v.body, '$.creditData.availableCreditLimit') AS available_credit_limit
FROM daily_sightings s
JOIN documents d ON d.id = s.document_id AND d.kind = 'accounts'
JOIN versions v ON v.sha256 = s.sha256""",
    # json_each over a null would yield one all-NULL row; the json_type guard
    # keeps accounts without reserves out instead of giving them a ghost reserve.
    """CREATE VIEW reserved_balance_history AS
SELECT run_id, day, observed_at, provider_updated_at, account_id, observation_id, position,
       reserve_id, stable_identity, reserve_name,
       CASE WHEN n = 1 THEN json_extract(entry, '$.availableAmounts[0].currencyCode') END
         AS currency,
       CASE WHEN n = 1 THEN json_extract(entry, '$.availableAmounts[0].amount') END AS amount,
       CASE WHEN n = 1 THEN json_extract(entry, '$.availableAmounts[0].remuneration.indexer') END
         AS indexer,
       CASE WHEN n = 1
         THEN json_extract(entry, '$.availableAmounts[0].remuneration.postFixedIndexerPercentage')
       END AS indexer_ratio,
       CASE WHEN n = 1
             AND json_extract(entry, '$.availableAmounts[0].remuneration.indexer') = 'CDI'
         THEN json_extract(entry, '$.availableAmounts[0].remuneration.postFixedIndexerPercentage')
              * 100
       END AS cdi_percentage,
       CASE WHEN n IS NULL THEN 'availableAmounts is not an array'
            WHEN n = 0 THEN 'no availableAmounts element'
            WHEN n > 1 THEN 'several availableAmounts elements'
            ELSE 'ok' END AS amount_status
FROM (
  SELECT s.run_id, s.day, s.observed_at, json_extract(v.body, '$.updatedAt') AS provider_updated_at,
         d.provider_id AS account_id, s.observation_id, r.key AS position, r.value AS entry,
         json_extract(r.value, '$.identification') AS reserve_id,
         CASE WHEN json_type(r.value, '$.identification') = 'text'
               AND json_extract(r.value, '$.identification') <> ''
               AND (SELECT count(*) FROM json_each(v.body, '$.bankData.reservedBalances') q
                    WHERE json_extract(q.value, '$.identification')
                        = json_extract(r.value, '$.identification')) = 1
              THEN 1 ELSE 0 END AS stable_identity,
         json_extract(r.value, '$.name') AS reserve_name,
         CASE WHEN json_type(r.value, '$.availableAmounts') = 'array'
              THEN json_array_length(r.value, '$.availableAmounts') END AS n
  FROM daily_sightings s
  JOIN documents d ON d.id = s.document_id AND d.kind = 'accounts'
  JOIN versions v ON v.sha256 = s.sha256,
  json_each(v.body, '$.bankData.reservedBalances') r
  WHERE json_type(v.body, '$.bankData.reservedBalances') = 'array'
)""",
    """CREATE VIEW investment_history AS
SELECT s.run_id, s.day, s.observed_at, json_extract(v.body, '$.updatedAt') AS provider_updated_at,
       json_extract(v.body, '$.date') AS provider_date,
       d.provider_id AS investment_id,
       json_extract(v.body, '$.name') AS name,
       json_extract(v.body, '$.currencyCode') AS currency,
       json_extract(v.body, '$.amountOriginal') AS amount_original,
       json_extract(v.body, '$.amount') AS amount_gross,
       json_extract(v.body, '$.taxes') AS taxes,
       json_extract(v.body, '$.taxes2') AS taxes2,
       json_extract(v.body, '$.amountWithdrawal') AS amount_net,
       json_extract(v.body, '$.rate') AS rate,
       json_extract(v.body, '$.rateType') AS rate_type,
       json_extract(v.body, '$.dueDate') AS due_date,
       json_extract(v.body, '$.status') AS status
FROM daily_sightings s
JOIN documents d ON d.id = s.document_id AND d.kind = 'investments'
JOIN versions v ON v.sha256 = s.sha256""",
    # A row for every sighting that differs from the previous one in body,
    # parent or ambiguity, or that has an event in its run (a reappearance with
    # an identical body is a row); identical consecutive sightings collapse.
    f"""CREATE VIEW transaction_versions AS
SELECT document_id, provider_id, account_id, run_id, day, observed_at, provider_updated_at,
       sha256, status, date, amount, currency, description, category, bill_post_date, events,
       ambiguous
FROM (
  SELECT o.document_id, d.provider_id, o.parent AS account_id, o.run_id, o.observed_day AS day,
         o.observed_at, json_extract(v.body, '$.updatedAt') AS provider_updated_at, o.sha256,
         json_extract(v.body, '$.status') AS status,
         json_extract(v.body, '$.date') AS date,
         json_extract(v.body, '$.amount') AS amount,
         json_extract(v.body, '$.currencyCode') AS currency,
         json_extract(v.body, '$.description') AS description,
         json_extract(v.body, '$.category') AS category,
         json_extract(v.body, '$.creditCardMetadata.billPostDate') AS bill_post_date,
         (SELECT group_concat(event, ',') FROM (
            SELECT e.event FROM events e
            WHERE e.run_id = o.run_id AND e.document_id = o.document_id ORDER BY e.seq)) AS events,
         {AMBIGUOUS_SQL} AS ambiguous,
         lag(o.sha256) OVER w AS previous_sha256,
         lag(o.parent) OVER w AS previous_parent,
         lag({AMBIGUOUS_SQL}) OVER w AS previous_ambiguous
  FROM observations o
  JOIN documents d ON d.id = o.document_id AND d.kind = 'transactions'
  JOIN versions v ON v.sha256 = o.sha256
  WINDOW w AS (PARTITION BY o.document_id ORDER BY o.run_id, o.id)
)
WHERE previous_sha256 IS NULL OR previous_sha256 <> sha256 OR previous_parent <> account_id
   OR previous_ambiguous <> ambiguous OR events IS NOT NULL""",
    # Coverage of every document today, against the projected run.
    f"""CREATE VIEW document_coverage AS
SELECT d.id AS document_id, d.kind, d.provider_id, ds.parent, ds.state, ds.ambiguous_run,
       {BUSINESS_DATE_SQL} AS business_date,
       c.last_absence_safe_run, c.last_min_date,
       CASE WHEN c.last_absence_safe_run IS NOT NULL
             AND c.last_absence_safe_run = (SELECT projected_run FROM projection_state WHERE id = 1)
             AND (d.kind NOT IN ('transactions', 'investment_transactions', 'bills')
                  OR (c.last_min_date IS NOT NULL AND {BUSINESS_DATE_SQL} >= c.last_min_date))
            THEN 1 ELSE 0 END AS covered
FROM document_state ds
JOIN documents d ON d.id = ds.document_id
JOIN versions v ON v.sha256 = ds.sha256
LEFT JOIN scope_coverage c ON c.kind = d.kind AND c.parent = ds.parent""",
    # A pending and a posted transaction on one account that look like the same
    # purchase. The provider gives no link; the view shows both and says it
    # cannot tell. Matched on the UTC calendar date because the time of day
    # moved by three hours in most pending-to-posted transitions.
    """CREATE VIEW pending_posted_candidates AS
SELECT p.account_id, p.id AS pending_id, p.date AS pending_date,
       p.fetched_at AS pending_observed_at, pc.covered AS pending_covered,
       q.id AS posted_id, q.date AS posted_date, q.fetched_at AS posted_observed_at,
       p.amount, p.currency, p.description
FROM transactions p
JOIN transactions q ON q.account_id = p.account_id AND q.id <> p.id
JOIN document_coverage pc ON pc.kind = 'transactions' AND pc.provider_id = p.id
JOIN document_coverage qc ON qc.kind = 'transactions' AND qc.provider_id = q.id
WHERE json_extract(p.raw, '$.status') = 'PENDING' AND json_extract(q.raw, '$.status') = 'POSTED'
  AND p.state = 'present' AND q.state = 'present'
  AND p.ambiguous_run IS NULL AND q.ambiguous_run IS NULL
  AND qc.covered = 1
  AND p.currency IS q.currency AND p.amount = q.amount AND p.description IS q.description
  AND date(p.date) = date(q.date)""",
]

VIEW_NAMES = (
    "run_coverage",
    "scope_coverage",
    "daily_sightings",
    "account_balance_history",
    "reserved_balance_history",
    "investment_history",
    "transaction_versions",
    "document_coverage",
    "pending_posted_candidates",
)
