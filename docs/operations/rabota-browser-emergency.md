# Rabota.md browser fallback: optional emergency profile

Production normally runs Rabota.md with `waf_http`, the A14 primary egress and the validated free-proxy reserve. Chromium is not installed in the standard image and `RABOTA_BROWSER_FALLBACK_MODE=none` is forced by `docker-compose.prod.yml`.

The browser fallback code is retained for emergency use. `docker-compose.browser.yml` replaces only the crawler `worker` with `jobhunter-prod-browser:<revision>`, builds that image with the `playwright` extra plus Chromium, and sets `RABOTA_BROWSER_FALLBACK_MODE=stealth_browser`. Other services keep the normal production image.

Enable the emergency crawler profile with:

```sh
sudo ./deploy/prod-browser-compose.sh up -d --build worker
```

Return to the standard browser-free crawler with:

```sh
sudo ./deploy/prod-compose.sh up -d --build worker
```

The source configuration should remain `transport: waf_http` and `fallback_transport: none`; the emergency profile is a runtime override, so activation does not require mutating the source row. Free proxies never receive Chromium fallback; their transport remains pure HTTP/WAF solver only.

Before enabling the emergency profile, check that the A14 egress is still reachable and inspect the Rabota/WAF diagnostics. Browser mode is intentionally not the default because a single Chromium session can add several hundred MiB of RAM and materially increase image/build size.


## Free-proxy reserve invariants

The standard browserless reserve is maintained every 15 minutes. A fully proven free proxy stays
`ready` for at least two maintenance intervals; the default is 1800 seconds. After that it may enter
a bounded revalidation grace window rather than being treated as dead immediately.

A single transient proof or transport failure during revalidation moves a previously proven proxy to
a non-serving `suspect` state with a short retry cooldown. Repeated failures eventually move it to
`dead`; explicit access rejection remains `banned`. An unproven `waf_candidate` that hits a
temporary WAF solve failure remains retryable instead of receiving the full dead cooldown immediately.
Suspect and candidate entries are never served to scans until a full landing + pagination preflight
succeeds again.

Reserve maintenance reports capacity explicitly:

- `ready`: ready count reached the configured target;
- `degraded`: at least one ready proxy exists, but the target was not reached;
- `empty`: no ready free proxy exists.

A successful Celery task therefore means the maintenance code executed, not that the reserve is
healthy. Operational checks must inspect the returned reserve outcome and ready count.


### Candidate quality and solver diagnostics

A free proxy is no longer promoted to `waf_candidate` merely because Rabota.md
returns `202 challenge`. The same egress must also fetch and parse the current
AWS WAF challenge metadata and `challenge.js`. This rejects proxies that can
reach Rabota.md but cannot carry the complete WAF protocol.

Token-backend exhaustion preserves the primary bounded failure taxonomy
(`transport`, `proof`, `protocol`, `rate_limit`) and WAF stage
(`landing`, `challenge_script`, `inputs`, `verify`). Maintenance logs and
Prometheus preflight metrics use those bounded values instead of collapsing
network failures into a generic `WafSolveFailed`.

Free-proxy sources accumulate Redis quality counters for challenge reachability,
fully proven egresses, transport failures and proof/protocol failures. Discovery
still samples every configured source, but higher-quality sources receive
priority within the bounded discovery budget.

The primary-path canary and the browserless pure-solver canary are independent.
The scheduled canary runs every four hours. A transport failure on one free
egress is inconclusive for solver compatibility; a proof/protocol failure
invalidates compatibility. The compatibility marker expires after six hours.
