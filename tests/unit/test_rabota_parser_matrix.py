from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from selectolax.parser import HTMLParser

from app.crawlers.adapters.rabota_md import (
    RabotaMdAccessDenied,
    RabotaMdAdapter,
    RabotaMdConfig,
    RabotaMdDegradedError,
    RabotaMdParseError,
    RabotaMdTemporaryError,
)
from app.crawlers.schemas import RawJobData, RawJobReference
from app.models.enums import JobStatus

BASE = "https://www.rabota.md"


class NeverFetcher:
    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        raise AssertionError(f"parser-only test unexpectedly requested {url}")


@pytest.fixture
def adapter() -> RabotaMdAdapter:
    return RabotaMdAdapter(
        RabotaMdConfig(live_mode=False),
        http_fetcher=NeverFetcher(),
    )


def job_html(
    *,
    job_id: str = "9001",
    title: str = "Operator suport clienți",
    company: str = "Example SRL",
    content: str = "Comunicare cu clienții și procesarea solicitărilor.",
    body_extra: str = "",
    before_main: str = "",
    head_extra: str = "",
    json_description: str | None = "Descriere JSON-LD de rezervă.",
) -> str:
    posting: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "hiringOrganization": {"@type": "Organization", "name": company},
    }
    if json_description is not None:
        posting["description"] = json_description
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <link rel="canonical" href="{BASE}/ru/locuri-de-munca/operator/{job_id}">
    <script type="application/ld+json">{json.dumps(posting, ensure_ascii=False)}</script>
    {head_extra}
  </head>
  <body>
    {before_main}
    <main>
      <h1 class="vacancy-title">{title}</h1>
      <a class="company-title" href="/ru/companies/example">{company}</a>
      <section class="vacancy-content">{content}</section>
      <div class="vacancy-city">Кишинев</div>
      {body_extra}
    </main>
  </body>
</html>"""


async def normalize(
    adapter: RabotaMdAdapter,
    html: str,
    *,
    job_id: str = "9001",
    locale: str = "ru",
    metadata: dict[str, Any] | None = None,
) -> Any:
    url = f"{BASE}/{locale}/locuri-de-munca/operator/{job_id}"
    reference = RawJobReference(
        external_id=job_id,
        url=url,
        locale=locale,
        category="others",
        metadata=metadata or {},
    )
    raw = RawJobData(
        reference=reference,
        html=html,
        final_url=url,
        fetched_at=datetime.now(UTC),
    )
    return await adapter.normalize_job(raw)


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        (
            '<div class="vacancy-contact"><a href="mailto:HR@Example.COM">CV</a></div>',
            "hr@example.com",
        ),
        (
            '<div class="vacancy-contacts">'
            '<a href="mailto:jobs@example.com?subject=CV">CV</a></div>',
            "jobs@example.com",
        ),
        (
            '<div data-job-contact><a href="mailto:jobs%2Bnight@example.com">CV</a></div>',
            "jobs+night@example.com",
        ),
        ('<div class="vacancy-content">Scrieți la plain@example.md</div>', "plain@example.md"),
        (
            '<div class="vacancy-description">Email: hr.office@example.co.uk.</div>',
            "hr.office@example.co.uk",
        ),
        ('<div class="vacancy-requirements">CV: apply@example.jobs</div>', "apply@example.jobs"),
        (
            '<div class="vacancy-responsibilities">CV: a_b-c@example.travel</div>',
            "a_b-c@example.travel",
        ),
        ('<a href="mailto:rabota@rabota.md">Support</a>', None),
        ('<a href="mailto:support@rabota.md">Support</a>', None),
        ('<a href="mailto:not-an-email">Broken</a>', None),
        ('<div class="vacancy-content">broken@example</div>', None),
        ('<div class="vacancy-content">name @ example.com</div>', None),
    ],
)
@pytest.mark.asyncio
async def test_employer_email_parsing_matrix(
    adapter: RabotaMdAdapter,
    markup: str,
    expected: str | None,
) -> None:
    job = await normalize(adapter, job_html(body_extra=markup))

    assert job.public_email == expected


@pytest.mark.asyncio
async def test_plain_vacancy_email_beats_unrelated_global_mailto(
    adapter: RabotaMdAdapter,
) -> None:
    html = job_html(
        before_main='<header><a href="mailto:marketing@portal.example">Portal</a></header>',
        content="Trimite CV la employer@example.md",
    )

    job = await normalize(adapter, html)

    assert job.public_email == "employer@example.md"


@pytest.mark.asyncio
async def test_script_and_style_emails_are_not_public_contacts(
    adapter: RabotaMdAdapter,
) -> None:
    content = (
        '<script>const recipient = "attacker@example.test";</script>'
        "Text fără adresă."
        "<style>.x{background:url(fake@example.test)}</style>"
    )

    job = await normalize(adapter, job_html(content=content))

    assert job.public_email is None


@pytest.mark.asyncio
async def test_source_service_email_is_skipped_before_real_global_contact(
    adapter: RabotaMdAdapter,
) -> None:
    html = job_html(
        before_main=(
            '<a href="mailto:rabota@rabota.md">Support</a>'
            '<div class="main-wrap"><a href="mailto:office@employer.md">Employer</a></div>'
        )
    )

    job = await normalize(adapter, html)

    assert job.public_email == "office@employer.md"


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('<a href="tel:+37322111222">Call</a>', "+37322111222"),
        ('<a href="tel:%2B373%2076%20123%20456">Call</a>', "+373 76 123 456"),
        ('<a href="tel:+37322111222?extension=4">Call</a>', "+37322111222"),
        ('<a href="tel:">Call</a>', None),
    ],
)
def test_phone_link_parsing_matrix(
    adapter: RabotaMdAdapter,
    markup: str,
    expected: str | None,
) -> None:
    tree = HTMLParser(f"<html><body>{markup}</body></html>")

    assert adapter._public_contact(tree, "tel") == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Telefon: 060 426 499", ["+37360426499"]),
        ("Oficiu: 022 123 456", ["+37322123456"]),
        ("Call +40 (721) 234-567", ["+40721234567"]),
        ("Call 00 44 20 7946 0958", ["+442079460958"]),
        ("Salariu 12000 lei", []),
        ("Electrician 12000 - 28000", []),
        ("Program 09.00 - 18.00. Telefon: 060 000 012", ["+37360000012"]),
        (
            "Salariu 11.100 lei. Telefon: 061000041 ----------------------- "
            "Зарплата 11 100 леев. Телефон: 061000041",
            ["+37361000041"],
        ),
        ("Call +55 40 7973 8696", []),
    ],
)
@pytest.mark.asyncio
async def test_phone_text_normalization_matrix(
    adapter: RabotaMdAdapter,
    content: str,
    expected: list[str],
) -> None:
    job = await normalize(adapter, job_html(content=content))

    assert job.public_phones == expected
    assert job.public_phone == (expected[0] if expected else None)


@pytest.mark.asyncio
async def test_global_source_phone_is_not_an_employer_contact(
    adapter: RabotaMdAdapter,
) -> None:
    html = job_html(
        before_main='<header><a href="tel:+37322921058">Portal support</a></header>',
        content="Telefon angajator: 060 426 499",
    )

    job = await normalize(adapter, html)

    assert job.public_phones == ["+37360426499"]


@pytest.mark.parametrize(
    ("raw", "minimum", "maximum", "currency"),
    [
        ("15 000 \u2013 20 000 леев", "15000", "20000", "MDL"),
        ("12.500 - 18.000 lei", "12500", "18000", "MDL"),
        ("12,500 - 18,000 MDL", "12500", "18000", "MDL"),
        ("1.250,50 \u2013 1.900,75 EUR", "1250.50", "1900.75", "EUR"),
        ("1,250.50 - 1,900.75 USD", "1250.50", "1900.75", "USD"),
        ("1000.50 USD", "1000.50", None, "USD"),
        ("500 €", "500", None, "EUR"),
        ("20\u00a0000\u200b\u2013\u00a030\u00a0000 lei", "20000", "30000", "MDL"),
        ("Salariu negociabil", None, None, None),
        ("Plată în USD", None, None, "USD"),
        (None, None, None, None),
    ],
)
def test_salary_parsing_matrix(
    raw: str | None,
    minimum: str | None,
    maximum: str | None,
    currency: str | None,
) -> None:
    parsed_minimum, parsed_maximum, parsed_currency = RabotaMdAdapter._parse_salary(raw)

    assert parsed_minimum == (Decimal(minimum) if minimum is not None else None)
    assert parsed_maximum == (Decimal(maximum) if maximum is not None else None)
    assert parsed_currency == currency


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-10 20:05:03", (2026, 8, 10, 20, 5, 3)),
        ("2026-08-10", (2026, 8, 10, 0, 0, 0)),
        ("2026-08-10T17:05:03+00:00", (2026, 8, 10, 17, 5, 3)),
        ("10 августа 2026", (2026, 8, 10, 0, 0, 0)),
        ("3 ianuarie 2025", (2025, 1, 3, 0, 0, 0)),
        ("29 februarie 2024", (2024, 2, 29, 0, 0, 0)),
        ("31 februarie 2026", None),
        ("10 unknown 2026", None),
        ("astăzi", None),
        (None, None),
    ],
)
def test_date_parsing_matrix(
    raw: str | None,
    expected: tuple[int, int, int, int, int, int] | None,
) -> None:
    parsed = RabotaMdAdapter._parse_date(raw)

    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (
            parsed.year,
            parsed.month,
            parsed.day,
            parsed.hour,
            parsed.minute,
            parsed.second,
        ) == (expected)
        assert parsed.tzinfo is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Remote", "remote"),
        ("Удалённая работа", "remote"),
        ("La distanță", "remote"),
        ("Hibrid", "hybrid"),
        ("Гибридный график", "hybrid"),
        ("În sediu", "onsite"),
        ("На территории работодателя", "onsite"),  # noqa: RUF001
        ("Работа в офисе", "onsite"),
        ("По городу", None),
        (None, None),
    ],
)
def test_workplace_type_matrix(raw: str | None, expected: str | None) -> None:
    assert RabotaMdAdapter._workplace_type(raw) == expected


@pytest.mark.parametrize(
    ("experience", "text", "expected"),
    [
        ("Без опыта", "", True),
        (None, "Опыт не требуется", True),
        ("Fără experiență", "", True),
        (None, "Se acceptă fara experienta", True),
        ("От 2 лет", "", False),
        ("С опытом", "Можно обучиться", False),  # noqa: RUF001
        (None, "Обычное описание", None),
    ],
)
def test_no_experience_matrix(
    experience: str | None,
    text: str,
    expected: bool | None,
) -> None:
    assert RabotaMdAdapter._no_experience(experience, text) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Кишинёв, Бельцы", ["Кишинёв", "Бельцы"]),
        ("Chișinău; Bălți / Orhei", ["Chișinău", "Bălți", "Orhei"]),
        ("Кишинёв, Кишинёв", ["Кишинёв"]),
        ("  Chișinău\u00a0 ;  Bălți ", ["Chișinău", "Bălți"]),
        (None, []),
    ],
)
def test_multi_value_matrix(raw: str | None, expected: list[str]) -> None:
    assert RabotaMdAdapter._multi_values(raw) == expected


@pytest.mark.parametrize(
    ("payload", "expected_title"),
    [
        ({"@type": "JobPosting", "title": "Direct"}, "Direct"),
        ({"@type": "jobposting", "title": "Case insensitive"}, "Case insensitive"),
        ({"@type": ["Thing", "JobPosting"], "title": "Typed list"}, "Typed list"),
        (
            {"@graph": [{"@type": "Organization"}, {"@type": "JobPosting", "title": "Graph"}]},
            "Graph",
        ),
        ([{"@type": "Thing"}, {"@type": "JobPosting", "title": "Array"}], "Array"),
    ],
)
def test_json_ld_shape_matrix(payload: Any, expected_title: str) -> None:
    tree = HTMLParser(
        f'<script type="application/ld+json">{json.dumps(payload, ensure_ascii=False)}</script>'
    )

    assert RabotaMdAdapter._job_posting_json_ld(tree)["title"] == expected_title


def test_invalid_json_ld_is_ignored() -> None:
    tree = HTMLParser('<script type="application/ld+json">{not-json}</script>')

    assert RabotaMdAdapter._job_posting_json_ld(tree) == {}


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/",
        f"{BASE}/ru/",
        f"{BASE}/ro/vacancies",
        f"{BASE}/ru/vacancies/category/others/2",
        f"{BASE}/ru/locuri-de-munca/operator/9001",
        "https://balti.rabota.md/ru/jobs",
    ],
)
def test_public_url_allowlist_matrix(adapter: RabotaMdAdapter, url: str) -> None:
    assert adapter._require_public_url(url).startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "http://www.rabota.md/ru/",
        "https://evil.example/ru/",
        "https://rabota.md.evil.example/ru/",
        "https://user@rabota.md/ru/",
        "https://rabota.md:8443/ru/",
        f"{BASE}/ru/login",
        f"{BASE}/ru/ajax/private",
        f"{BASE}/ru/cabinet/",
        f"{BASE}/en/jobs",
        f"{BASE}/ru/locuri-de-munca/not-a-job/no-id",
    ],
)
def test_public_url_rejection_matrix(adapter: RabotaMdAdapter, url: str) -> None:
    with pytest.raises(RabotaMdAccessDenied):
        adapter._require_public_url(url)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://careers.example.com/apply/42", "https://careers.example.com/apply/42"),
        ("https://careers.example.com/apply/42#form", "https://careers.example.com/apply/42"),
        ("http://careers.example.com/apply/42", None),
        ("https://user@careers.example.com/apply/42", None),
        ("/ru/locuri-de-munca/operator/9001", None),
        ("javascript:alert(1)", None),
        ("#apply", None),
    ],
)
@pytest.mark.asyncio
async def test_external_application_url_matrix(
    adapter: RabotaMdAdapter,
    href: str,
    expected: str | None,
) -> None:
    markup = f'<a class="external-application" href="{href}">Apply</a>'

    job = await normalize(adapter, job_html(body_extra=markup))

    assert job.application_url == expected


@pytest.mark.parametrize(
    ("content", "extra", "json_description", "expected"),
    [
        ("Вакансия закрыта", "", "fallback", JobStatus.CLOSED),
        ("Anunțul nu mai este activ", "", "fallback", JobStatus.CLOSED),
        ("", '<div class="vacancy-as-image"><img src="job.png"></div>', None, JobStatus.INCOMPLETE),
        ("", "", None, JobStatus.INCOMPLETE),
        ("Descriere completă și actuală.", "", "fallback", JobStatus.ACTIVE),
    ],
)
@pytest.mark.asyncio
async def test_job_status_matrix(
    adapter: RabotaMdAdapter,
    content: str,
    extra: str,
    json_description: str | None,
    expected: JobStatus,
) -> None:
    job = await normalize(
        adapter,
        job_html(content=content, body_extra=extra, json_description=json_description),
    )

    assert job.status == expected


@pytest.mark.asyncio
async def test_canonical_and_alternate_links_reject_wrong_job_ids(
    adapter: RabotaMdAdapter,
) -> None:
    head = (
        f'<link rel="canonical" href="{BASE}/ru/locuri-de-munca/wrong/9999">'
        f'<link rel="alternate" hreflang="ro" href="{BASE}/ro/locuri-de-munca/operator/9999">'
        '<link rel="alternate" hreflang="en" '
        f'href="{BASE}/en/locuri-de-munca/operator/9001">'
    )

    job = await normalize(adapter, job_html(head_extra=head))

    assert job.canonical_url == f"{BASE}/ru/locuri-de-munca/operator/9001"
    assert job.localized_urls == {"ru": f"{BASE}/ru/locuri-de-munca/operator/9001"}


@pytest.mark.asyncio
async def test_reference_localized_urls_accept_only_supported_matching_publications(
    adapter: RabotaMdAdapter,
) -> None:
    metadata = {
        "localized_urls": {
            "ro": f"{BASE}/ro/locuri-de-munca/operator/9001",
            "ru": f"{BASE}/ru/locuri-de-munca/wrong/9999",
            "en": f"{BASE}/en/locuri-de-munca/operator/9001",
            "unsafe": "https://evil.example/job/9001",
        }
    }

    job = await normalize(adapter, job_html(), metadata=metadata)

    assert job.localized_urls == {
        "ru": f"{BASE}/ru/locuri-de-munca/operator/9001",
        "ro": f"{BASE}/ro/locuri-de-munca/operator/9001",
    }


@pytest.mark.asyncio
async def test_content_hash_is_deterministic_and_changes_with_contact(
    adapter: RabotaMdAdapter,
) -> None:
    first = await normalize(
        adapter,
        job_html(content="Descriere stabilă. Email: first@example.md"),
    )
    repeated = await normalize(
        adapter,
        job_html(content="Descriere stabilă. Email: first@example.md"),
    )
    changed = await normalize(
        adapter,
        job_html(content="Descriere stabilă. Email: second@example.md"),
    )

    assert first.content_hash == repeated.content_hash
    assert first.content_hash != changed.content_hash
    assert first.source_fingerprint == changed.source_fingerprint


def test_listing_reference_filters_private_cross_domain_and_inactive_links(
    adapter: RabotaMdAdapter,
) -> None:
    html = """
    <div class="job-card"><a href="/ru/locuri-de-munca/valid/1001">Valid</a></div>
    <a href="/inactive/ru/locuri-de-munca/old/1002">Inactive</a>
    <a href="https://evil.example/ru/locuri-de-munca/evil/1003">Cross domain</a>
    <a href="/ru/ajax/locuri-de-munca/private/1004">Private</a>
    """

    references = adapter._references_from_listing(
        html,
        f"{BASE}/ru/vacancies/category/others",
        category="others",
        region=None,
    )

    assert [item.external_id for item in references] == ["1001"]


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        (
            '<a rel="next" href="/ru/vacancies/category/others/2">Next</a>',
            f"{BASE}/ru/vacancies/category/others/2",
        ),
        (
            '<div data-next="/ru/vacancies/category/others/3"></div>',
            f"{BASE}/ru/vacancies/category/others/3",
        ),
        (
            '<a href="/ru/vacancies/category/others/4">Следующая</a>',
            f"{BASE}/ru/vacancies/category/others/4",
        ),
        ('<a rel="next" href="https://evil.example/page/2">Next</a>', None),
        ('<a rel="next" href="/ru/locuri-de-munca/job/9001">Next</a>', None),
    ],
)
def test_next_page_matrix(
    adapter: RabotaMdAdapter,
    markup: str,
    expected: str | None,
) -> None:
    page = f"{BASE}/ru/vacancies/category/others"

    assert adapter._next_page_url(markup, page, {page}) == expected


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (200, None),
        (301, None),
        (399, None),
        (400, RabotaMdParseError),
        (404, RabotaMdParseError),
        (403, RabotaMdDegradedError),
        (429, RabotaMdDegradedError),
        (500, RabotaMdTemporaryError),
        (503, RabotaMdTemporaryError),
    ],
)
def test_http_status_classification_matrix(
    status: int,
    error: type[Exception] | None,
) -> None:
    request = httpx.Request("GET", f"{BASE}/ru/")
    response = httpx.Response(status, request=request)

    if error is None:
        RabotaMdAdapter._require_success(response, str(request.url))
    else:
        with pytest.raises(error):
            RabotaMdAdapter._require_success(response, str(request.url))


@pytest.mark.parametrize(
    ("path", "body", "blocked"),
    [
        ("/ru/", "<html>Normal page</html>", False),
        ("/ru/login", "<html>Login</html>", True),
        ("/ru/", '<div class="captcha-box">Check</div>', True),
        ("/ru/", '<div id="captcha">Check</div>', True),
        ("/ru/", "window.awsWafCookieDomainList = [];", True),
        ("/ru/", "window.gokuProps = {};", True),
        ("/ru/", "cf-chl-widget", True),
        ("/ru/", "Cloudflare Ray ID 123", True),
        ("/ru/", "Подтвердите, что вы человек", True),
        ("/ru/", "VERIFY YOU ARE HUMAN", True),
    ],
)
def test_challenge_detection_matrix(path: str, body: str, blocked: bool) -> None:
    request = httpx.Request("GET", f"{BASE}{path}")
    response = httpx.Response(200, text=body, request=request)

    if blocked:
        with pytest.raises(RabotaMdDegradedError):
            RabotaMdAdapter._detect_challenge(response)
    else:
        RabotaMdAdapter._detect_challenge(response)


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('<a data-caid="9001">Send</a>', True),
        ('<a href="/vacancies/send_response">Send</a>', True),
        ("<button>Отправить CV</button>", True),
        ("<button>Отправить резюме</button>", True),
        ("<button>Trimite CV</button>", True),
        ("<button>Aplică</button>", True),
        ("<button>Aplica</button>", True),
        ("<button>Сохранить</button>", False),
        ('<a href="https://careers.example/apply">External</a>', False),
    ],
)
def test_internal_application_control_matrix(markup: str, expected: bool) -> None:
    assert RabotaMdAdapter._internal_application_available(HTMLParser(markup)) is expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/ru/jobs", f"{BASE}/ru/jobs"),
        ("../jobs", None),
        ("#fragment", None),
        ("mailto:hr@example.md", None),
        ("tel:+37322111222", None),
        ("data:text/html,hello", None),
        ("javascript:alert(1)", None),
        ("/ru/%61jax/private", None),
        ("//evil.example/ru/jobs", None),
    ],
)
def test_candidate_public_url_matrix(
    adapter: RabotaMdAdapter,
    href: str,
    expected: str | None,
) -> None:
    assert adapter._candidate_public_url(f"{BASE}/ru/vacancies", href) == expected
