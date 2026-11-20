-- Migration 168: Persist the raw external trigger payload on the request row.
--
-- A webhook workflow materialises its request at receipt time, but the task
-- that consumes the payload can be dispatched minutes later, by a different
-- process. Until now the only copy was rendered prose inside
-- requests.description / tasks.description, which is lossy (capped at 8000
-- characters, re-serialised) and not machine-readable. This adds one nullable
-- JSONB slot on requests that holds the body as received plus a small
-- descriptor:
--
--   {"kind": "json"|"text",
--    "content_type": "<Content-Type header value>",
--    "byte_size": <int, length of the received body>,
--    "payload": <parsed JSON value | decoded body text>}
--
-- Headers and credentials are deliberately excluded. The workflow token
-- arrives in an Authorization / X-Lucent-Workflow-Token header or the ?token=
-- query string and is never written here.
--
-- Purely additive: the column is nullable with NO default, so PostgreSQL does
-- not rewrite the table, no backfill runs, and every existing row reads NULL —
-- exactly what a request not created by an external trigger sees. Application
-- code treats NULL as "no trigger payload" and behaves as it does today.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS trigger_payload JSONB;

COMMENT ON COLUMN requests.trigger_payload IS
    'Raw external trigger payload (webhook body) plus descriptor, written at receipt so a later task dispatch still has it. NULL for requests not created by an external trigger.';
