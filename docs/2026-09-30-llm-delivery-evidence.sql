-- Investigation-only SELECTs. No identifiers, recipient addresses, or bodies are exported.
-- Cohort matches the operator's original 1289 attempts, not the moving live totals.
-- Use an existing authenticated PostgreSQL connection; never put credentials here.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '15s';

SELECT count(*) AS attempts,
       count(*) FILTER (WHERE outcome = 'success') AS successful_http,
       count(*) FILTER (WHERE outcome <> 'success') AS failed_attempts,
       count(DISTINCT logical_request_id) AS logical_requests,
       count(DISTINCT logical_request_id) FILTER (WHERE outcome <> 'success') AS affected,
       count(DISTINCT logical_request_id) FILTER (WHERE recovered) AS recovered,
       count(DISTINCT logical_request_id) FILTER (
           WHERE is_final_attempt AND outcome <> 'success' AND NOT recovered
       ) AS transport_final_failed
FROM external_call_events
WHERE occurred_at >= TIMESTAMPTZ '2026-09-29 21:00:00+00'
  AND occurred_at <= TIMESTAMPTZ '2026-09-30 17:06:52.862215+00';

SELECT provider, resource, outcome, http_status, exception_type, count(*) AS attempts
FROM external_call_events
WHERE occurred_at >= TIMESTAMPTZ '2026-09-29 21:00:00+00'
  AND occurred_at <= TIMESTAMPTZ '2026-09-30 17:06:52.862215+00'
GROUP BY provider, resource, outcome, http_status, exception_type
ORDER BY attempts DESC;

WITH requests AS (
    SELECT logical_request_id, count(*) AS attempts,
           count(*) FILTER (WHERE outcome = 'success') AS successes,
           sum(latency_ms) AS summed_attempt_ms
    FROM external_call_events
    WHERE occurred_at >= TIMESTAMPTZ '2026-09-29 21:00:00+00'
      AND occurred_at <= TIMESTAMPTZ '2026-09-30 17:06:52.862215+00'
    GROUP BY logical_request_id
)
SELECT min(attempts), max(attempts), round(avg(attempts), 3) AS mean_attempts,
       sum(summed_attempt_ms) AS total_attempt_ms,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY summed_attempt_ms) AS p95_attempt_ms,
       count(*) FILTER (WHERE successes > 1) AS requests_with_multiple_http_successes,
       sum(successes - 1) AS additional_http_successes
FROM requests;

WITH latest AS (
    SELECT DISTINCT ON (profile_id, source_job_id) risks, created_at
    FROM match_evaluations
    WHERE created_at <= TIMESTAMPTZ '2026-09-30 17:06:53+00'
    ORDER BY profile_id, source_job_id, created_at DESC, id DESC
)
SELECT risks::text, count(*) AS unresolved_matching
FROM latest
WHERE created_at >= TIMESTAMPTZ '2026-09-29 21:00:00+00'
  AND risks::text LIKE '%llm_provider_failure:%'
GROUP BY risks::text;

-- Bound scores prove send authority; a later matching result cannot replace them.
SELECT count(*) AS submitted,
       count(*) FILTER (WHERE d.provider_accepted_at IS NOT NULL) AS originally_accepted,
       count(*) FILTER (WHERE d.status = 'provider_accepted') AS currently_accepted,
       count(*) FILTER (WHERE d.status = 'domain_rejected') AS currently_domain_rejected,
       count(*) FILTER (WHERE a.status = 'sent') AS applications_sent,
       count(*) FILTER (WHERE a.status = 'failed') AS applications_failed,
       count(*) FILTER (WHERE m.id IS NULL) AS missing_evaluation,
       min(m.overall_fit) AS minimum_bound_score,
       max(m.overall_fit) AS maximum_bound_score,
       count(*) FILTER (WHERE m.overall_fit = 0) AS zero_bound_score,
       count(*) FILTER (WHERE m.risks::text LIKE '%llm_provider_failure:%') AS invalid_bound,
       count(*) FILTER (WHERE a.policy_decision = 'auto_approved') AS automatic,
       count(*) FILTER (WHERE a.policy_result::jsonb->>'catchup_stage' IS NOT NULL) AS catchup,
       count(DISTINCT a.profile_id) AS profiles
FROM email_deliveries d
JOIN applications a ON a.id = d.application_id
LEFT JOIN match_evaluations m ON m.id = a.match_evaluation_id
WHERE d.submitted_at >= TIMESTAMPTZ '2026-09-29 21:00:00+00'
  AND d.submitted_at <= TIMESTAMPTZ '2026-09-30 17:06:53+00';

-- Reproduce historical score substitution without reading any letter or job text.
SELECT bound.overall_fit AS bound_score, bound.created_at AS bound_at,
       latest.overall_fit AS report_score, latest.created_at AS latest_at,
       a.sent_at, a.policy_result::jsonb->>'policy_version' AS policy_version,
       a.policy_result::jsonb->'rules_failed' AS failed_rules,
       a.policy_result::jsonb->'catchup_stage' AS catchup_stage,
       a.policy_result::jsonb->'minimum_remaining' AS minimum_remaining
FROM email_deliveries d
JOIN applications a ON a.id = d.application_id
JOIN match_evaluations bound ON bound.id = a.match_evaluation_id
JOIN LATERAL (
    SELECT overall_fit, created_at
    FROM match_evaluations m
    WHERE m.profile_id = a.profile_id AND m.canonical_job_id = a.canonical_job_id
      AND m.created_at <= TIMESTAMPTZ '2026-09-30 17:06:53+00'
    ORDER BY m.created_at DESC, m.id DESC LIMIT 1
) latest ON true
WHERE d.submitted_at >= TIMESTAMPTZ '2026-09-29 21:00:00+00'
  AND d.submitted_at <= TIMESTAMPTZ '2026-09-30 17:06:53+00'
  AND latest.overall_fit = 0;

SELECT d.status, d.failure_class, d.smtp_status,
       d.submitted_at, d.provider_accepted_at, d.bounced_at, d.attempt_count,
       d.next_retry_at, a.status AS application_status, a.sent_at,
       c.verification_status, c.delivery_state, c.last_failure_reason
FROM email_deliveries d
JOIN applications a ON a.id = d.application_id
JOIN employer_contacts c ON c.id = a.recipient_contact_id
WHERE d.submitted_at >= TIMESTAMPTZ '2026-09-29 21:00:00+00'
  AND d.submitted_at <= TIMESTAMPTZ '2026-09-30 17:06:53+00'
  AND d.status = 'domain_rejected';

COMMIT;
