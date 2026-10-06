<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

## Project checks

Use Python 3.12 semantics. Before handing off changes run `ruff check .`, `ruff format --check .`,
`mypy app fixture_site`, and `pytest`. Never enable real email delivery or live crawling in tests.

## Adding job sources

Adding an adapter is not a completed source integration. Also provide idempotent registration
of its `JobSource` record in the target database and verify that the source appears in the
administrator and user settings panels. Include registration in the rollout plan and report
the resulting source names and operational states. New sources start disabled with automatic
actions paused unless the operator explicitly authorizes enabling them. Preserve existing
source configuration, profile choices, and operational states when registering missing sources.
Never mark an integration complete based only on adapter registry membership or image deployment.

For built-in sites, attach a `SourceDefinition` to the adapter registration in the default
registry. The API startup and `seed` reconcile this catalog automatically. Keep its stable
`catalog_key` unchanged across code refactors; defaults must not overwrite existing operator
configuration or acknowledge/re-enable an existing source without an explicit operator action.
Use `scripts/verify.sh` for reproducible browser-enabled checks. Include a catalog/startup-to-panel
regression scenario when changing source registration, rather than asserting only a fixed list
of source names. Apply schema migrations before starting an API with changed models.

## Production deployment gate

Develop and validate JobHunter changes in the local WSL checkout by default. Do not edit files,
pull commits, build images, run migrations, restart services, or deploy changes on the production
server while implementing or debugging a change.

### Required Samsung A51 DEV deployment

The dedicated DEV target is the rooted Samsung A51, ADB serial `100.123.23.6:39285`.
Use its operator-installed native Docker kernel/runtime; do not start the old emulated VM.
The ADB port can change after reboot: discover the connected endpoint on `100.123.23.6`
and verify `SM-A515F` and root before operating. Never substitute another phone or the
workstation Docker daemon. Preserve unrelated containers/images on the shared native daemon.
Building images, installing the isolated JobHunter DEV runtime, applying DEV migrations,
deploying, restarting and testing this DEV environment are explicitly authorized and must
be performed without requesting operator permission. This authorization does not include
PROD, PhoneGate, unrelated phone services, changing the phone kernel, or disabling SELinux.
Keep DEV Docker images, containers, databases, queues, volumes and credentials separate
from PROD and from other applications on the phone. Never copy PROD secrets or databases.

A51 and A14 are physically adjacent and share Wi-Fi as well as Tailscale. Consider a
verified direct LAN path for A51's primary proxy on A14; do not assume that the shared
subnet permits peer traffic or that A14's loopback SOCKS listener is LAN-accessible.
Discover current addresses and verify connectivity/listener exposure before configuring
a route. Keep the existing A14 proxy/tunnel and PhoneGate unchanged under DEV authorization.
Changing an A14 production listener, allowlist or tunnel requires its own validated rollout
and explicit authorization. Default offline DEV checks do not use the live A14 proxy.

Every DEV surface must identify itself as a DEV version: all browser pages (including login,
registration and both panels), page titles, API/OpenAPI, health/status and MCP status.
Real email delivery, live crawling, PhoneGate actions and other external actions remain
disabled in default DEV acceptance checks; use isolated fixture/mock providers.

Before asking for PROD deployment permission, build and deploy the exact candidate to A51
and thoroughly verify that deployed revision: migrations, health/readiness, API, worker/Beat,
source registration, ownership/authentication, relevant complete workflows and regression
tests. Browser/integration-sensitive workflows must pass through Playwright in at least
three consecutive clean browser contexts against the DEV deployment. Record the revision,
image digests and verification evidence; a local-only or mocked route check cannot replace
verification of the actual DEV deployment. Then report results and rollout/rollback plan
before requesting PROD authorization. The production testing gate below still applies.

If A51 DEV deployment cannot be completed or verified (ADB/root unavailable, unsupported
kernel/Docker, storage/memory pressure, build/startup/test failure), immediately notify the
operator with the concrete blocker and evidence. Continue independent safe preparation,
but do not claim DEV acceptance, silently substitute a different host, or request a normal
PROD rollout before the required DEV gate succeeds. An emergency override requires the
operator's explicit instruction.

Before any production mutation, the exact changed behavior must pass the relevant focused tests,
the project checks above, and an end-to-end test in a local or non-production environment. Browser,
authentication, OAuth, redirect, cookie, and other integration-sensitive changes must be exercised
through Playwright with a clean browser context and must complete successfully at least three
consecutive times. Mocked provider responses and narrow route tests do not replace this end-to-end
gate. A production read-only diagnostic may help reproduce a bug, but a successful production test
must never be used as a substitute for pre-deployment validation.

After the evidence is collected, report the tests and proposed rollout/rollback plan to the operator.
Mutate production only after explicit deployment authorization in the current conversation. Earlier
authorization does not carry forward to later changes, and ordinary deployment authorization does
not waive this testing gate; bypassing it requires the operator to explicitly declare an emergency
override.


## Cross-service PhoneGate isolation

JobHunter DEV agents must never modify `/srv/phonegate/.env`, rotate PhoneGate production secrets, or restart `phonegate.service`. Treat PhoneGate as an external production dependency. Live tests may use an operator-injected `PHONEGATE_AUTH_TOKEN` only after explicit authorization; never create a "DEV token" by changing PhoneGate production credentials. If a PhoneGate secret may have leaked, stop and report it; rotation belongs to the PhoneGate deployment workflow.

## Docker image and disk hygiene

Do not leave Docker archaeology after builds or deployments. After a JobHunter rollout, `api`, `worker`, and `beat` must converge on the same intended application image digest; do not leave long-running services split across old generations.

After every successful rebuild/deploy: verify service health and image digests, check `docker system df` and `df -h /`, remove obsolete unused JobHunter application images/dangling layers, and prune unused build cache when it is not intentionally retained. Keep at most one previous JobHunter image only for an explicit rollback window, then remove it.

Never use `docker system prune -a` as a routine cleanup step. Never prune named volumes, PostgreSQL data, resume data, or unrelated on-demand images such as Talkies/Qdrant just to improve the percentage. Treat `/` above 70% after deployment as a problem to investigate before starting another image build.

## Git workflow

Treat this repository as the source of truth for JobHunter code and deployment configuration.
Before editing, inspect `git status` and the relevant `git diff` so existing work is not overwritten.
Keep changes focused and create an explanatory commit after a completed, validated change. Do not
rewrite, amend, squash, reset, or discard unrelated existing work unless the operator explicitly asks.

Never commit local secrets or credentials, including `.env`, `.admin-password`, `.mcp-token`, private
keys, OAuth/client secrets, API keys, database dumps, or generated credential files. Before the first
commit and whenever adding sensitive configuration, verify ignored files with `git check-ignore` and
review the staged file list. Keep secret examples as placeholders only.

For production changes, prefer deploying code that is represented by a commit. Record/check the
current commit when diagnosing a rollout, verify the deployed image contains the intended change,
and leave the working tree clean after a successful deployment unless there is deliberate unfinished
work that must remain visible in `git status`.

For DEV/PROD code synchronization, `/home/andrei/JobHunter:refs/heads/main` is the canonical DEV
reference. Never use the currently checked-out DEV `HEAD` for synchronization health because the
working checkout may legitimately be on a feature branch. A dirty DEV working tree is not itself a
sync failure. Standard production updates must move `/srv/jobhunter-prod:main` only by fast-forward
to the exact canonical DEV main commit. Do not create normal commits or cherry-picks in the PROD
checkout and never push from PROD back into DEV. An explicit emergency override may temporarily
depart from this flow, but canonical DEV main must be reconciled before normal deployment resumes.
