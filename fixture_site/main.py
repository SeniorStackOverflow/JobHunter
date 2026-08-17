from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from fixture_site.data import FixtureJob, jobs_for_phase

app = FastAPI(title="Job Agent Fixture Site", docs_url=None, redoc_url=None)
app.state.phase = 1


def _localized_title(job: FixtureJob, locale: str) -> str:
    return job.title_ro if locale == "ro" else job.title_en


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/__control__/phase/{phase}")
async def set_phase(phase: int) -> dict[str, int]:
    if phase not in {1, 2, 3}:
        raise HTTPException(status_code=400, detail="phase must be 1, 2 or 3")
    app.state.phase = phase
    return {"phase": phase}


@app.get("/{locale}/categories", response_class=HTMLResponse)
async def categories(locale: str) -> str:
    if locale not in {"en", "ro"}:
        raise HTTPException(status_code=404)
    values = sorted(
        {category for job in jobs_for_phase(app.state.phase) for category in job.categories}
    )
    links = "".join(
        (
            f'<li><a class="category" data-category="{escape(item)}" '
            f'href="/{locale}/jobs?category={escape(item)}">'
            f"{escape(item.title())}</a></li>"
        )
        for item in values
    )
    return f"<html><body><main><ul>{links}</ul></main></body></html>"


@app.get("/{locale}/regions", response_class=HTMLResponse)
async def regions(locale: str) -> str:
    if locale not in {"en", "ro"}:
        raise HTTPException(status_code=404)
    values = sorted({region for job in jobs_for_phase(app.state.phase) for region in job.regions})
    links = "".join(
        (
            f'<a class="region" data-region="{escape(item)}" '
            f'href="/{locale}/jobs?region={escape(item)}">'
            f"{escape(item.title())}</a>"
        )
        for item in values
    )
    return f"<html><body><nav>{links}</nav></body></html>"


@app.get("/{locale}/jobs", response_class=HTMLResponse)
async def listing(
    locale: str,
    category: str | None = None,
    region: str | None = None,
    cursor: int = Query(default=0, ge=0),
    mirror: bool = False,
) -> str:
    if locale not in {"en", "ro"}:
        raise HTTPException(status_code=404)
    jobs = [job for job in jobs_for_phase(app.state.phase) if not job.closed]
    jobs = [job for job in jobs if job.mirror] if mirror else jobs
    if category:
        jobs = [job for job in jobs if category in job.categories]
    if region:
        jobs = [job for job in jobs if region in job.regions]
    page_size = 3
    page = jobs[cursor : cursor + page_size]
    cards = "".join(
        f'''<section class="opening" data-key="{escape(job.job_id)}">
        <h2><a class="opening-link" href="/{locale}/job/{escape(job.slug)}">
        {escape(_localized_title(job, locale))}</a></h2>
        <span class="org">{escape(job.company)}</span>
        <span class="place">{escape(job.regions[0])}</span>
        <time class="changed">{escape(job.updated)}</time></section>'''
        for job in page
    )
    next_link = ""
    if cursor + page_size < len(jobs):
        query: dict[str, str | int] = {"cursor": cursor + page_size}
        if category:
            query["category"] = category
        if region:
            query["region"] = region
        if mirror:
            query["mirror"] = "true"
        next_link = f'<a class="more" rel="next" href="/{locale}/jobs?{urlencode(query)}">More</a>'
    return f'<html><body><div id="openings">{cards}</div>{next_link}</body></html>'


@app.get("/{locale}/job/{slug}", response_class=HTMLResponse)
async def detail(locale: str, slug: str) -> str:
    if locale not in {"en", "ro"}:
        raise HTTPException(status_code=404)
    job = next((item for item in jobs_for_phase(app.state.phase) if item.slug == slug), None)
    if job is None:
        raise HTTPException(status_code=404)
    if job.closed:
        raise HTTPException(status_code=410, detail="job is closed")
    alternate = "ro" if locale == "en" else "en"
    email = (
        f'<a class="apply-email" href="mailto:{escape(job.email)}">{escape(job.email)}</a>'
        if job.email
        else ""
    )
    salary = f'<dd class="pay">{escape(job.salary)}</dd>' if job.salary else ""
    categories = ",".join(job.categories)
    structured_title = escape(_localized_title(job, locale))
    structured_company = escape(job.company)
    return f'''<!doctype html><html lang="{locale}"><head>
    <link rel="canonical" href="/{locale}/job/{escape(job.slug)}">
    <link rel="alternate" hreflang="{alternate}" href="/{alternate}/job/{escape(job.slug)}">
    <script type="application/ld+json">{{"@type":"JobPosting",
    "title":"{structured_title}","datePosted":"{job.published}",
    "dateModified":"{job.updated}",
    "hiringOrganization":{{"name":"{structured_company}"}}}}</script>
    </head><body><article data-job-id="{escape(job.job_id)}" data-categories="{escape(categories)}">
    <h1 class="role">{structured_title}</h1>
    <a class="employer">{structured_company}</a>
    <div class="job-copy">{escape(job.description)}</div><dl>{salary}
    <dd class="where">{escape(", ".join(job.regions))}</dd>
    <dd class="hours">{escape(job.schedule)}</dd></dl>
    <time class="published">{job.published}</time><time class="updated">{job.updated}</time>{email}
    <a class="official-apply" href="https://apply.example.test/{escape(job.job_id)}">Apply</a>
    </article></body></html>'''
