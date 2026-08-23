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

## Production deployment gate

Develop and validate JobHunter changes in the local WSL checkout by default. Do not edit files,
pull commits, build images, run migrations, restart services, or deploy changes on the production
server while implementing or debugging a change.

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
