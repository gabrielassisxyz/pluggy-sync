-- A synthetic legacy database frozen after run 2. Run 1 was truncated and every
-- row it wrote was overwritten by run 2 a minute later, so run 1 survives only in
-- sync_runs and probes. Values are placeholders; nothing here came from a real account.
INSERT INTO sync_runs (id, started_at, finished_at, api_calls, note) VALUES
  (1, '2026-09-12T18:51:23+00:00', '2026-09-12T18:51:33+00:00', 7, NULL),
  (2, '2026-09-12T18:52:23+00:00', '2026-09-12T18:52:33+00:00', 7, NULL);

INSERT INTO probes (run_id, path, status, rows, note, fetched_at) VALUES
  (1, '/categories', 200, 2, '', '2026-09-12T18:51:23+00:00'),
  (1, '/items/item-0001', 200, 1, '', '2026-09-12T18:51:24+00:00'),
  (1, '/identity?itemId=item-0001', 200, 1, '', '2026-09-12T18:51:25+00:00'),
  (1, '/investments?itemId=item-0001', 200, 1, '', '2026-09-12T18:51:26+00:00'),
  (1, '/accounts?itemId=item-0001', 200, 1, '', '2026-09-12T18:51:27+00:00'),
  (1, '/v2/transactions?accountId=account-0001', 200, 1, '', '2026-09-12T18:51:33+00:00'),
  (2, '/categories', 200, 2, '', '2026-09-12T18:52:23+00:00'),
  (2, '/items/item-0001', 200, 1, '', '2026-09-12T18:52:24+00:00'),
  (2, '/identity?itemId=item-0001', 200, 1, '', '2026-09-12T18:52:25+00:00'),
  (2, '/investments?itemId=item-0001', 200, 1, '', '2026-09-12T18:52:26+00:00'),
  (2, '/accounts?itemId=item-0001', 200, 1, '', '2026-09-12T18:52:27+00:00'),
  (2, '/v2/transactions?accountId=account-0001', 200, 2, '', '2026-09-12T18:52:33+00:00');

INSERT INTO categories (id, description, parent_id, raw, fetched_at) VALUES
  ('01000000', 'Income', NULL, '{"id": "01000000", "description": "Income", "parentId": null}', '2026-09-12T18:52:23+00:00'),
  ('01000001', 'Salary', '01000000', '{"id": "01000001", "description": "Salary", "parentId": "01000000"}', '2026-09-12T18:52:23+00:00');

INSERT INTO items (id, connector, status, execution_status, last_updated, next_auto_sync, raw, fetched_at) VALUES
  ('item-0001', 'Synthetic Bank', 'UPDATED', 'SUCCESS', '2026-09-12T10:00:00.000Z', NULL,
   '{"id": "item-0001", "connector": {"id": 1, "name": "Synthetic Bank"}, "status": "UPDATED", "executionStatus": "SUCCESS", "lastUpdatedAt": "2026-09-12T10:00:00.000Z"}',
   '2026-09-12T18:52:24+00:00');

INSERT INTO identities (id, item_id, document, full_name, raw, fetched_at) VALUES
  ('identity-0001', 'item-0001', '00000000000', 'Placeholder Person',
   '{"id": "identity-0001", "itemId": "item-0001", "document": "00000000000", "fullName": "Placeholder Person"}',
   '2026-09-12T18:52:25+00:00');

INSERT INTO investments (id, item_id, type, subtype, name, balance, currency, raw, fetched_at) VALUES
  ('investment-0001', 'item-0001', 'FIXED_INCOME', 'CDB', 'CDB placeholder', 1000.0, 'BRL',
   '{"id": "investment-0001", "type": "FIXED_INCOME", "subtype": "CDB", "name": "CDB placeholder", "balance": 1000.0, "currencyCode": "BRL", "amount": 1000.0}',
   '2026-09-12T18:52:26+00:00');

INSERT INTO investment_transactions (id, investment_id, date, amount, raw, fetched_at) VALUES
  ('movement-0001', 'investment-0001', '2026-09-01T00:00:00.000Z', 1000.0,
   '{"id": "movement-0001", "date": "2026-09-01T00:00:00.000Z", "amount": 1000.0, "type": "BUY"}',
   '2026-09-12T18:52:26+00:00');

INSERT INTO accounts (id, item_id, type, subtype, name, balance, currency, raw, fetched_at) VALUES
  ('account-0001', 'item-0001', 'BANK', 'CHECKING_ACCOUNT', 'checking', 100.0, 'BRL',
   '{"id": "account-0001", "type": "BANK", "subtype": "CHECKING_ACCOUNT", "name": "checking", "balance": 100.0, "currencyCode": "BRL"}',
   '2026-09-12T18:52:27+00:00');

INSERT INTO transactions (id, account_id, date, description, amount, currency, type, category, raw, fetched_at) VALUES
  ('tx-0001', 'account-0001', '2026-09-10T12:00:00.000Z', 'purchase one', -10.0, 'BRL', 'DEBIT', 'Shopping',
   '{"id": "tx-0001", "date": "2026-09-10T12:00:00.000Z", "description": "purchase one", "amount": -10.0, "currencyCode": "BRL", "type": "DEBIT", "category": "Shopping", "status": "POSTED"}',
   '2026-09-12T18:52:33+00:00'),
  ('tx-pending', 'account-0001', '2026-09-08T15:00:00.000Z', 'pending purchase', -50.36, 'BRL', 'DEBIT', NULL,
   '{"id": "tx-pending", "date": "2026-09-08T15:00:00.000Z", "description": "pending purchase", "amount": -50.36, "currencyCode": "BRL", "type": "DEBIT", "status": "PENDING"}',
   '2026-09-12T18:52:33+00:00');

-- Consents were never probed by the legacy collector; the row still exists.
INSERT INTO consents (id, item_id, expires_at, revoked_at, raw, fetched_at) VALUES
  ('consent-0001', 'item-0001', '2027-01-01T00:00:00Z', NULL,
   '{"id": "consent-0001", "itemId": "item-0001", "expiresAt": "2027-01-01T00:00:00Z"}',
   '2026-09-12T18:52:24+00:00');
