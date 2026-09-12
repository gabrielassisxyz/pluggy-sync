# pluggy-sync

Pulls everything a Pluggy account will hand over into one local SQLite file.

No dependencies beyond the Python standard library, no install step, no build.
One script, one database.

## Running it

Three environment variables, the same three Pluggy itself documents:

```
PLUGGY_CLIENT_ID       from the Pluggy dashboard
PLUGGY_CLIENT_SECRET   from the Pluggy dashboard
PLUGGY_ITEM_IDS        connection ids, comma-separated
```

```sh
./pluggy_sync.py
```

The database lands beside the script as `pluggy.db`, or wherever `PLUGGY_DB`
points. Re-running is safe: every row is upserted by its provider id, so a
second run refreshes what changed and adds what is new.

## What it stores, and why it stores it twice

Every table keeps the provider's JSON verbatim in a `raw` column. The extracted
columns next to it — date, amount, description, balance — exist so the common
queries can use an index, not to replace the document they came from.

That redundancy is the point. Provider-side enrichment is not permanent:
transaction categories arrive while a plan includes them and stop when it does
not, and a field dropped at capture time cannot be fetched again later. Filtering
belongs in the query, never in the ingest.

Tables: `items`, `accounts`, `transactions`, `bills`, `investments`,
`investment_transactions`, `consents`, `identities`, `categories`.

Two more record the run itself. `sync_runs` holds one row per invocation with its
API call count. `probes` holds one row per endpoint touched, with the status it
answered — including the ones that refused, because "this connector does not
publish identity" is a finding worth keeping rather than an error worth hiding.

## What the API will not do

- **No listing of connections.** Pluggy publishes no endpoint that enumerates
  items, deliberately: a leaked key cannot discover what it can read. A
  connection whose id is not in `PLUGGY_ITEM_IDS` is invisible, and no amount of
  code fixes that.
- **No refresh on demand.** On the MeuPluggy connector, `PATCH /items/{id}` is
  refused outright. Freshness is whatever the provider's own daily sync last
  produced; `items.last_updated` and `items.next_auto_sync` record it.
- **Read only.** Nothing here can move money or change anything at a bank.

History runs about twelve months back, which is what Open Finance retains.

## Scheduling

The provider syncs roughly every 24 hours and prohibits batch processes against
the API. Once a day, after `next_auto_sync` has passed, is the whole of it —
anything more frequent re-reads the same rows.
