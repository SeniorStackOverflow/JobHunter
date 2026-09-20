from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

HTTP_REQUESTS = Counter(
    "job_agent_http_requests_total",
    "HTTP requests handled by the API.",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "job_agent_http_request_duration_seconds",
    "HTTP request duration without high-cardinality URL labels.",
    ("method", "route"),
)
CELERY_TASKS = Counter(
    "job_agent_celery_tasks_total",
    "Celery tasks by stable task name and terminal state.",
    ("task", "state"),
)
CELERY_TASK_DURATION = Histogram(
    "job_agent_celery_task_duration_seconds",
    "Celery task execution duration.",
    ("task",),
)
SCAN_RUNS = Counter(
    "job_agent_scan_runs_total",
    "Completed scan runs by type and state.",
    ("scan_type", "state"),
)
SCAN_JOBS = Counter(
    "job_agent_scan_jobs_total",
    "Jobs observed by scan outcome.",
    ("outcome",),
)
SCAN_ERRORS = Counter(
    "job_agent_scan_errors_total",
    "Crawler errors by bounded class.",
    ("kind",),
)
SOURCE_HEALTH = Gauge(
    "job_agent_source_health",
    "Current source health (1 for the current state).",
    ("source_id", "state"),
)
APPLICATIONS = Counter(
    "job_agent_applications_total",
    "Application pipeline outcomes.",
    ("state",),
)
EMAIL_DELIVERIES = Counter(
    "job_agent_email_deliveries_total",
    "Email delivery outcomes.",
    ("provider", "state"),
)
RABOTA_WAF_TOKEN_REFRESH = Counter(
    "job_agent_rabota_md_waf_token_refresh_total",
    "aws-waf-token refreshes by backend outcome.",
    ("outcome",),
)
RABOTA_WAF_CHALLENGE = Counter(
    "job_agent_rabota_md_waf_challenge_total",
    "AWS WAF 202 challenge responses observed by the HTTP transport.",
)
RABOTA_TRANSPORT_FALLBACK = Counter(
    "job_agent_rabota_md_transport_fallback_total",
    "In-scan switches from waf_http to the stealth browser.",
    ("reason",),
)
RABOTA_PROXY_EGRESS = Counter(
    "job_agent_rabota_md_proxy_egress_total",
    "Rabota.md proxy egress selection and health outcomes.",
    ("kind", "outcome"),
)
RABOTA_PROXY_FAILOVER = Counter(
    "job_agent_rabota_md_proxy_failover_total",
    "Rabota.md egress failovers by previous egress kind and bounded reason.",
    ("from_kind", "reason"),
)
RABOTA_PROXY_POOL_ALIVE = Gauge(
    "job_agent_rabota_md_proxy_pool_alive",
    "Fresh free-proxy reserve entries; WAF candidates are not yet full-protocol ready.",
)
WAF_SOLVER_ATTEMPTS = Counter(
    "job_agent_waf_solver_attempts_total",
    "Pure-Python AWS WAF solver attempts.",
)
WAF_SOLVER_SUCCESS = Counter(
    "job_agent_waf_solver_success_total",
    "Successful pure-Python AWS WAF solves.",
)
WAF_SOLVER_SOLVE_DURATION = Histogram(
    "job_agent_waf_solver_solve_duration_seconds",
    "Pure-Python AWS WAF solve duration.",
)
WAF_SOLVER_COMPATIBILITY = Gauge(
    "job_agent_waf_solver_compatibility",
    "Whether a fresh live WAF protocol compatibility canary is present (1=yes).",
)
RABOTA_TRANSPORT_REQUESTS = Counter(
    "job_agent_rabota_md_transport_requests_total",
    "Successful Rabota.md logical requests by serving transport.",
    ("transport",),
)
RABOTA_HTTP_WITHOUT_BROWSER_RATIO = Gauge(
    "job_agent_rabota_md_http_without_browser_ratio",
    "Per-fetcher ratio of successful logical requests served without Chromium.",
)
WAF_SOLVER_CANARY = Counter(
    "job_agent_waf_solver_canary_total",
    "Daily WAF solver canary outcomes.",
    ("outcome",),
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


__all__ = [
    "APPLICATIONS",
    "CELERY_TASKS",
    "CELERY_TASK_DURATION",
    "EMAIL_DELIVERIES",
    "HTTP_REQUESTS",
    "HTTP_REQUEST_DURATION",
    "RABOTA_HTTP_WITHOUT_BROWSER_RATIO",
    "RABOTA_PROXY_EGRESS",
    "RABOTA_PROXY_FAILOVER",
    "RABOTA_PROXY_POOL_ALIVE",
    "RABOTA_TRANSPORT_FALLBACK",
    "RABOTA_TRANSPORT_REQUESTS",
    "RABOTA_WAF_CHALLENGE",
    "RABOTA_WAF_TOKEN_REFRESH",
    "SCAN_ERRORS",
    "SCAN_JOBS",
    "SCAN_RUNS",
    "SOURCE_HEALTH",
    "WAF_SOLVER_ATTEMPTS",
    "WAF_SOLVER_CANARY",
    "WAF_SOLVER_COMPATIBILITY",
    "WAF_SOLVER_SOLVE_DURATION",
    "WAF_SOLVER_SUCCESS",
    "metrics_response",
]
