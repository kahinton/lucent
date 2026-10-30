-- Migration 161: collapse duplicate report cards to one current card per request.
--
-- Report cards were appended on every request status change, so a finished
-- request left BOTH a "ready for review" card and a "completed" card in the
-- transcript. Nothing ever retracted the earlier one, so a person (and the model
-- reading the thread) saw finished work described as awaiting review.
--
-- Cards are a projection of a request's CURRENT state, not an audit trail.
-- `LLMSessionRepository.upsert_report_card` now enforces one card per request.
-- This migration brings existing rows in line with that invariant by keeping
-- only the newest card for each request and hiding the rest.
--
-- Rows are hidden (`visible = FALSE`), never deleted: `llm_session_events` stays
-- append-only for audit purposes, and the transcript already filters on
-- `visible`. Hidden rows also stop re-appearing on reload.
--
-- Scoped narrowly: only event_type = 'subagent_report' and only rows that share a
-- request_id with a newer card.

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY session_id, raw->>'request_id'
               ORDER BY sequence DESC
           ) AS rn
    FROM llm_session_events
    WHERE event_type = 'subagent_report'
      AND visible = TRUE
      AND raw->>'request_id' IS NOT NULL
)
UPDATE llm_session_events e
SET visible = FALSE
FROM ranked
WHERE e.id = ranked.id
  AND ranked.rn > 1;
