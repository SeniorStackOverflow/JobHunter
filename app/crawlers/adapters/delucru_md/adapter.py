# ruff: noqa: RUF001
from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from selectolax.parser import HTMLParser

from app.crawlers.adapters.delucru_md.errors import (
    DelucruMdAccessDenied,
    DelucruMdDegradedError,
    DelucruMdError,
    DelucruMdParseError,
    DelucruMdTemporaryError,
)
from app.crawlers.http import HttpFetcher, SecureHttpClient
from app.crawlers.schemas import (
    AccessPolicyResult,
    JobRecheckResult,
    NormalizedJobData,
    RawJobData,
    RawJobReference,
    ScanCheckpoint,
    SourceCategoryData,
    SourceLocale,
    SourceRegion,
    SourceValidationResult,
)
from app.models.entities import JobSource
from app.models.enums import JobStatus
from app.phone.numbers import normalize_e164
from app.security.ssrf import Resolver
from app.settings import get_settings

_SUPPORTED_LOCALES = ("ro", "ru")
_JOB_PATH_RE = re.compile(
    r"^/(?:(ru|ro)/)?job/(?:[^/?#]*-)?(?P<job_id>[0-9]+)(?:/)?$",
    re.IGNORECASE,
)
_CATEGORY_PATH_RE = re.compile(
    r"^/(?:(ru|ro)/)?jobs/(?P<category>[a-z0-9_-]+)(?:/)?$",
    re.IGNORECASE,
)
_CLOSED_MARKERS = (
    "anunțul a expirat",
    "anuntul a expirat",
    "объявление устарело",
    "acest loc de muncă nu mai este activ",
    "acest loc de munca nu mai este activ",
    "вакансия закрыта",
    "вакансия больше не активна",
    "объявление больше не активно",
    "nu mai este activ",
)
_PUBLIC_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
    re.IGNORECASE,
)
_DELUCRU_SERVICE_EMAILS = {"este@delucru.md", "info@delucru.md", "support@delucru.md"}
_DELUCRU_SERVICE_PHONES = {"+37379008345", "+37376709778"}
_INTERNAL_ACTION_PARTS = (
    "/jobs/form/",
    "/jobs/click/",
    "/candidate/",
    "/jobseeker/",
    "/employer/",
    "/auth/",
    "/login",
    "/register",
)
_CONTENT_HASH_VERSION = 2

_CHISINAU_DISTRICTS: dict[str, str] = {
    "botanica": "Botanica",
    "buiucani": "Buiucani",
    "centru": "Centru",
    "ciocana": "Ciocana",
    "râșcani": "Râșcani",
    "rascani": "Râșcani",
    "poșta veche": "Poșta Veche",
    "posta veche": "Poșta Veche",
    "telecentru": "Telecentru",
    "sculeni": "Sculeni",
    "рышкановка": "Râșcani",
    "чеканы": "Ciocana",
    "буюканы": "Buiucani",
    "ботаника": "Botanica",
    "центр": "Centru",
    "старая почта": "Poșta Veche",
    "телецентр": "Telecentru",
    "скулянка": "Sculeni",
}

_CITY_CANONICAL_MAP: dict[str, str] = {
    "chișinău": "Chișinău",
    "chisinau": "Chișinău",
    "кишинёв": "Chișinău",
    "кишинев": "Chișinău",
    "bălți": "Bălți",
    "balti": "Bălți",
    "бельцы": "Bălți",
    "cahul": "Cahul",
    "кахул": "Cahul",
    "ungheni": "Ungheni",
    "унгены": "Ungheni",
    "orhei": "Orhei",
    "орхей": "Orhei",
    "strășeni": "Strășeni",
    "straseni": "Strășeni",
    "страшены": "Strășeni",
    "ialoveni": "Ialoveni",
    "яловены": "Ialoveni",
    "comrat": "Comrat",
    "комрат": "Comrat",
    "edineț": "Edineț",
    "edinet": "Edineț",
    "единцы": "Edineț",
    "soroca": "Soroca",
    "сороки": "Soroca",
    "florești": "Florești",
    "флорешты": "Florești",
    "glodeni": "Glodeni",
    "глодяны": "Glodeni",
    "șoldănești": "Șoldănești",
    "солданешты": "Șoldănești",
    "cimișlia": "Cimișlia",
    "чимишлия": "Cimișlia",
    "căușeni": "Căușeni",
    "кэушень": "Căușeni",
    "hîncești": "Hîncești",
    "хинчешты": "Hîncești",
    "găgăuzia": "Găgăuzia",
    "гагаузия": "Găgăuzia",
}

# 46 canonical categories on delucru.md
_KNOWN_CATEGORIES: list[tuple[str, str, str]] = [
    ("acquisitions", "Achiziții / Import / Export", "Закупки / Импорт / Экспорт"),
    ("naval-aeronautic", "Aeronautic / Naval", "Авиация / Флот"),
    ("agriculture", "Agricultură / Mediu / Ecologie", "Сельское хозяйство / Экология"),
    ("food-industry-horeca", "Alimentație / HoReCa", "Общепит / Рестораны"),
    (
        "architecture-interior-design",
        "Arhitectură / Proiectare / Design Interior",
        "Архитектура / Дизайн интерьера",
    ),
    ("social-work-psychology", "Asistență Socială / Psihologie", "Социальная работа / Психология"),
    ("automotive-equipment", "Auto / Echipamente", "Автосервис / Оборудование"),
    ("chemistry-biochemistry", "Chimie / Biochimie", "Химия / Биохимия"),
    ("clothing-clothing-design", "Confecții / Design Vestimentar", "Пошив одежды / Моделирование"),
    (
        "construction-repairs-installations",
        "Construcții / Reparații / Instalații",
        "Строительство / Ремонт",
    ),
    ("quality-control", "Controlul Calității", "Контроль качества"),
    ("euducation-training-courses", "Educație / Training / Cursuri", "Образование / Тренинги"),
    (
        "crewing-casino-entertainment-show-business",
        "Entertainment / Show / Cultură / Artă",
        "Развлечения / Шоу / Культура",
    ),
    (
        "finances",
        "Finanțe / Bănci / Creditare / Contabilitate",
        "Финансы / Бухгалтерия / Банки",
    ),
    ("public-service", "Funcții publice", "Государственная служба"),
    ("graphics-webdesign-dtp", "Grafică / Webdesign / DTP", "Графика / Веб-дизайн"),
    ("imobiliare", "Imobiliare", "Недвижимость"),
    ("engineering", "Inginerie / Statistică / Matematică", "Инженерия / Статистика"),
    (
        "au-pair-babysitting-house-cleaning",
        "Îngrijire / Personal la domiciliu",
        "Домашний персонал",
    ),
    ("law", "Juridic / Asigurări", "Юриспруденция / Страхование"),
    ("journalism-media-publishing", "Jurnalism / Mass-media / Editorial", "Журналистика / СМИ"),
    ("foreign-languages-translating", "Limbi străine / Traduceri", "Иностранные языки / Переводы"),
    (
        "logistics-warehouse-work-administration",
        "Logistică / Depozit / Administrativ",
        "Логистика / Склад",
    ),
    (
        "marketing-advertisment-pr",
        "Marketing / Publicitate / E-Commerce",
        "Маркетинг / Реклама / PR",
    ),
    ("medicine-pharmacy", "Medicină și Farmaceutică", "Медицина / Фармацевтика"),
    ("merchandising-promoting", "Merchandising / Promoteri", "Мерчендайзинг / Промоутеры"),
    ("auxiliary-workers", "Muncitori auxiliari", "Разнорабочие"),
    (
        "office-back-office-secretary-work",
        "Office / Back-office / Secretariat",
        "Офис / Секретариат",
    ),
    ("ngo", "ONG", "НПО / Некоммерческие организации"),
    ("security-and-protection-military", "Pază și protecție / Militar", "Охрана / Безопасность"),
    ("service-staff", "Personal de serviciu / Curățenie", "Уборка / Обслуживающий персонал"),
    ("oil-gas", "Petrol / Gaze", "Нефть / Газ"),
    ("wood-processing-pvc", "Prelucrarea lemnului / PVC", "Обработка дерева / ПВХ"),
    ("production", "Producție / Producere", "Производство"),
    ("it-software", "Tech / IT / Programare", "Технологии / IT / Программирование"),
    ("it-internet", "IT / Internet", "IT / Интернет"),
    ("lucru-de-acasa-part-time", "Lucru de acasă / Part-time", "Работа на дому / Частичная"),
    ("sales-consulting", "Vânzări / Consultanță", "Продажи / Консультации"),
    ("transport-auto", "Transport / Auto", "Транспорт / Авто"),
    ("telecomunicatii", "Telecomunicații", "Телекоммуникации"),
    ("studenti-fara-experienta", "Studenți / Fără experiență", "Студенты / Без опыта"),
    ("top-management", "Top Management", "Топ-менеджмент"),
    ("resurse-umane", "Resurse Umane / HR", "Кадры / HR"),
    ("turism-hoteluri", "Turism / Hoteluri", "Туризм / Гостиницы"),
    ("frumusete-fitness-sport", "Frumusețe / Fitness / Sport", "Красота / Фитнес / Спорт"),
    (
        "instalatii-sanitare-climatizare",
        "Instalații Sanitare / Climatizare",
        "Сантехника / Вентиляция",
    ),
    ("altele", "Altele / Diverse", "Другое / Разное"),
]


def _default_locales() -> list[Literal["ro", "ru"]]:
    return ["ro", "ru"]


def _default_incremental_categories() -> list[str]:
    return ["it-software", "it-internet", "lucru-de-acasa-part-time", "sales-consulting"]


class DelucruMdConfig(BaseModel):
    """Runtime configuration for the dedicated public Delucru.md adapter."""

    model_config = ConfigDict(frozen=True)

    base_url: str = "https://www.delucru.md"
    live_mode: bool = True
    policy_review_acknowledged: bool = False
    policy_review_reference: str | None = None
    locale_priority: list[Literal["ro", "ru"]] = Field(default_factory=_default_locales)
    requests_per_minute: int = Field(default=25, ge=1, le=60)
    minimum_interval_seconds: float = Field(default=2.0, ge=1.0)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_pages_per_entrypoint: int = Field(default=100, ge=1, le=1_000)
    incremental_max_pages_per_entrypoint: int = Field(default=10, ge=1, le=1_000)
    incremental_known_detail_refresh_hours: int = Field(default=72, ge=1, le=168)
    incremental_refresh_jitter_hours: int = Field(default=12, ge=0, le=72)
    incremental_detail_refresh_budget: int = Field(default=50, ge=1, le=1_000)
    known_unchanged_stop_threshold: int = Field(default=50, ge=1, le=100_000)
    max_discovered_entrypoints: int = Field(default=10_000, ge=1, le=100_000)
    incremental_category_slugs: list[str] = Field(default_factory=_default_incremental_categories)
    user_agent: str = "job-agent/0.1"
    proxy_url: str | None = None

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.netloc:
            raise ValueError(
                "proxy_url must be an http(s) or socks5 URL with a valid host and port"
            )
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"delucru.md", "www.delucru.md"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("DelucruMdAdapter base_url must be https://[www.]delucru.md")
        return value.rstrip("/")

    @field_validator("locale_priority")
    @classmethod
    def unique_locales(cls, value: list[Literal["ro", "ru"]]) -> list[Literal["ro", "ru"]]:
        if not value or len(set(value)) != len(value):
            raise ValueError("locale_priority must contain unique supported locales")
        return value

    @model_validator(mode="after")
    def require_policy_review_reference(self) -> DelucruMdConfig:
        if (
            self.live_mode
            and self.policy_review_acknowledged
            and not (self.policy_review_reference or "").strip()
        ):
            raise ValueError(
                "policy_review_reference is required when live policy review is acknowledged"
            )
        return self

    @field_validator("user_agent")
    @classmethod
    def identifying_user_agent(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or cleaned.lower() in {"mozilla/5.0", "curl", "python-httpx"}:
            raise ValueError("an identifying crawler User-Agent is required")
        return cleaned


class DelucruMdAdapter:
    """Dedicated high-fidelity crawler adapter for public Delucru.md vacancies.

    Delucru.md has direct Nginx origin hosting without Cloudflare or third-party WAF,
    using IP rate-limiting and plaintext contact masks inside the DOM.
    """

    adapter_type = "delucru_md"
    capabilities: ClassVar[list[str]] = [
        "dynamic_locales",
        "dynamic_categories",
        "dynamic_regions",
        "full_scan",
        "incremental_scan",
        "recheck",
        "zero_click_contacts",
        "localized_job_merge",
    ]

    def __init__(
        self,
        config: DelucruMdConfig | JobSource | dict[str, Any] | None = None,
        *,
        http_fetcher: HttpFetcher | None = None,
        client: HttpFetcher | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        if http_fetcher is not None and client is not None:
            raise ValueError("provide only one of http_fetcher or client")
        self.source: JobSource | None = None
        if isinstance(config, JobSource):
            self.source = config
            configured = config.configuration.get("source", config.configuration)
            raw_config = dict(configured) if isinstance(configured, dict) else {}
            raw_config.setdefault("base_url", config.base_url)
            rate_limit = (
                config.rate_limit if getattr(config, "rate_limit", None) is not None else 25
            )
            raw_config.setdefault("requests_per_minute", min(rate_limit, 60))
            raw_config.setdefault("user_agent", get_settings().crawler_user_agent)
            incremental = raw_config.get("incremental_scan")
            if isinstance(incremental, dict):
                raw_config.setdefault(
                    "incremental_max_pages_per_entrypoint",
                    incremental.get("max_pages_per_entrypoint", 10),
                )
                raw_config.setdefault(
                    "known_unchanged_stop_threshold",
                    incremental.get("known_unchanged_stop_threshold", 50),
                )
                raw_config.setdefault(
                    "incremental_category_slugs",
                    incremental.get("category_slugs", _default_incremental_categories()),
                )
                for field, nested_field, default in (
                    ("incremental_known_detail_refresh_hours", "known_detail_refresh_hours", 72),
                    ("incremental_refresh_jitter_hours", "refresh_jitter_hours", 12),
                    ("incremental_detail_refresh_budget", "detail_refresh_budget", 50),
                ):
                    raw_config.setdefault(field, incremental.get(nested_field, default))
            parsed_config = DelucruMdConfig.model_validate(raw_config)
        elif isinstance(config, DelucruMdConfig):
            parsed_config = config
        else:
            raw_config = config or {}
            nested_config = raw_config.get("source", raw_config)
            parsed_config = DelucruMdConfig.model_validate(nested_config)
        self.config = parsed_config
        injected_fetcher = http_fetcher or client
        if not self.config.live_mode and injected_fetcher is None:
            raise ValueError(
                "DelucruMdConfig.live_mode=false is a test-fixture mode and requires an "
                "explicitly injected HttpFetcher"
            )
        self._owns_http = injected_fetcher is None
        if injected_fetcher is not None:
            self._http = injected_fetcher
        else:
            proxy_transport = (
                httpx.AsyncHTTPTransport(
                    proxy=self.config.proxy_url,
                    limits=httpx.Limits(max_keepalive_connections=0),
                )
                if self.config.proxy_url
                else None
            )
            self._http = SecureHttpClient(
                allowed_domains=["delucru.md", "www.delucru.md"],
                user_agent=self.config.user_agent,
                requests_per_minute=self.config.requests_per_minute,
                minimum_interval_seconds=self.config.minimum_interval_seconds,
                timeout_seconds=self.config.timeout_seconds,
                max_redirects=self.config.max_redirects,
                resolver=resolver,
                transport=proxy_transport,
                pin_resolved_addresses=False if self.config.proxy_url else None,
            )

        self._access_result: AccessPolicyResult | None = None
        self._locale_cache: list[SourceLocale] | None = None
        self._category_cache: list[SourceCategoryData] | None = None
        self._region_cache: list[SourceRegion] | None = None
        self._references_by_id: dict[str, RawJobReference] = {}
        self.last_checkpoint = ScanCheckpoint()

    def set_incremental_categories(self, slugs: list[str]) -> None:
        cleaned = list(dict.fromkeys(slug.strip().casefold() for slug in slugs))
        if any(not re.fullmatch(r"[a-z0-9_-]+", slug) for slug in cleaned):
            raise ValueError(
                "category slugs must contain only letters, digits, underscores or hyphens"
            )
        self.config = self.config.model_copy(update={"incremental_category_slugs": cleaned})

    async def __aenter__(self) -> DelucruMdAdapter:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        close = getattr(self._http, "aclose", None)
        try:
            if self._owns_http and close is not None:
                await close()
        finally:
            self._references_by_id.clear()
            self._locale_cache = None
            self._category_cache = None
            self._region_cache = None
            self._access_result = None
            self.last_checkpoint = ScanCheckpoint()

    async def validate_source(self) -> SourceValidationResult:
        policy = await self.check_access_policy()
        if not policy.allowed:
            return SourceValidationResult(
                valid=False,
                errors=[policy.reason],
                capabilities=self.capabilities,
            )
        try:
            locales = await self.discover_locales()
            for locale in locales:
                response = await self._get_public_page(locale.start_urls[0])
                self._require_success(response, locale.start_urls[0])
                self._validate_listing(response.text)
        except (DelucruMdError, httpx.HTTPError) as exc:
            return SourceValidationResult(
                valid=False,
                errors=[str(exc)],
                capabilities=self.capabilities,
            )
        warnings: list[str] = []
        discovered = {item.code for item in locales}
        missing = [locale for locale in self.config.locale_priority if locale not in discovered]
        if missing:
            warnings.append(f"configured locales not discovered: {', '.join(missing)}")
        return SourceValidationResult(
            valid=bool(locales),
            errors=[] if locales else ["no supported public locales discovered"],
            warnings=warnings,
            capabilities=self.capabilities,
        )

    async def check_access_policy(self) -> AccessPolicyResult:
        checked_at = datetime.now(UTC)
        terms_url = f"{self.config.base_url}/terms"
        if self.config.live_mode and not self.config.policy_review_acknowledged:
            result = AccessPolicyResult(
                allowed=False,
                terms_url=terms_url,
                reason="delucru.md live crawling requires explicit policy review acknowledgment",
                checked_at=checked_at,
            )
            self._access_result = result
            return result
        result = AccessPolicyResult(allowed=True, terms_url=terms_url, checked_at=checked_at)
        self._access_result = result
        return result

    async def discover_locales(self) -> list[SourceLocale]:
        if self._locale_cache is not None:
            return list(self._locale_cache)
        locales: list[SourceLocale] = []
        for code in self.config.locale_priority:
            prefix = "" if code == "ro" else f"/{code}"
            name = "Română" if code == "ro" else "Русский"
            locales.append(
                SourceLocale(
                    code=code,
                    name=name,
                    start_urls=[f"{self.config.base_url}{prefix}/jobs"],
                )
            )
        self._locale_cache = locales
        return list(locales)

    async def discover_categories(self) -> list[SourceCategoryData]:
        if self._category_cache is not None:
            return list(self._category_cache)
        categories: list[SourceCategoryData] = []
        for locale in await self.discover_locales():
            prefix = "" if locale.code == "ro" else f"/{locale.code}"
            cat_url = f"{self.config.base_url}{prefix}/jobs/by-category"
            response = await self._get_public_page(cat_url)
            self._require_success(response, cat_url)
            if response.status_code == 200:
                tree = HTMLParser(response.text)
                for a in tree.css("a[href*='/jobs/']"):
                    href = a.attributes.get("href") or ""
                    cand = self._candidate_public_url(cat_url, href)
                    if not cand or "/by-" in cand:
                        continue
                    m = _CATEGORY_PATH_RE.match(urlsplit(cand).path)
                    if m:
                        slug = m.group("category").lower()
                        if slug not in {"jobs", "by-category", "by-city", "by-district"}:
                            name = self._clean_text(a.text(separator=" ", strip=True))
                            # Strip trailing job count e.g. "(180)" or " 293 joburi"
                            name = re.sub(
                                r"\s*(?:\(\d+\)|\d+\s*(?:joburi|вакансий|вакансии|locuri|oferte))\s*$",
                                "",
                                name,
                                flags=re.IGNORECASE,
                            ).strip()
                            if not name:
                                name = slug
                            categories.append(
                                SourceCategoryData(
                                    external_id=slug,
                                    name=name,
                                    url=cand,
                                    locale=locale.code,
                                )
                            )

        if not categories:
            # Fallback to known 46 categories
            for slug, ro_name, ru_name in _KNOWN_CATEGORIES:
                for locale in await self.discover_locales():
                    prefix = "" if locale.code == "ro" else f"/{locale.code}"
                    name = ru_name if locale.code == "ru" else ro_name
                    categories.append(
                        SourceCategoryData(
                            external_id=slug,
                            name=name,
                            url=f"{self.config.base_url}{prefix}/jobs/{slug}",
                            locale=locale.code,
                        )
                    )

        deduped: dict[tuple[str, str], SourceCategoryData] = {}
        for c in categories:
            deduped[(c.external_id, c.locale)] = c
        self._category_cache = list(deduped.values())
        return list(self._category_cache)

    async def discover_regions(self) -> list[SourceRegion]:
        if self._region_cache is not None:
            return list(self._region_cache)
        regions: list[SourceRegion] = []
        for locale in await self.discover_locales():
            prefix = "" if locale.code == "ro" else f"/{locale.code}"
            for subpage in ("/jobs/by-city", "/jobs/by-district"):
                reg_url = f"{self.config.base_url}{prefix}{subpage}"
                response = await self._get_public_page(reg_url)
                self._require_success(response, reg_url)
                if response.status_code == 200:
                    tree = HTMLParser(response.text)
                    for a in tree.css("a[href*='/jobs/']"):
                        href = a.attributes.get("href") or ""
                        cand = self._candidate_public_url(reg_url, href)
                        if not cand or "/by-" in cand:
                            continue
                        m = _CATEGORY_PATH_RE.match(urlsplit(cand).path)
                        if m:
                            slug = m.group("category").lower()
                            name = self._clean_text(a.text(separator=" ", strip=True))
                            name = re.sub(
                                r"\s*(?:\(\d+\)|\d+\s*(?:joburi|вакансий|вакансии|locuri|oferte))\s*$",
                                "",
                                name,
                                flags=re.IGNORECASE,
                            ).strip()
                            regions.append(
                                SourceRegion(
                                    external_id=slug,
                                    name=name or slug,
                                    url=cand,
                                    locale=locale.code,
                                )
                            )

        if not regions:
            for dist_slug, dist_name in _CHISINAU_DISTRICTS.items():
                if dist_slug.isascii():
                    regions.append(
                        SourceRegion(
                            external_id=dist_slug,
                            name=dist_name,
                            url=f"{self.config.base_url}/jobs/{dist_slug}",
                            locale="ro",
                        )
                    )
        deduped_reg = {(r.external_id, r.locale): r for r in regions}
        self._region_cache = list(deduped_reg.values())
        return list(self._region_cache)

    def iterate_full_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]:
        return self._iterate_scan(checkpoint, incremental=False)

    def iterate_incremental_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]:
        return self._iterate_scan(checkpoint, incremental=True)

    async def fetch_job_details(self, reference: RawJobReference) -> RawJobData:
        await self._ensure_access()
        url = self._require_public_url(reference.url)
        response = await self._get_public_page(url)
        self._require_success(response, url)
        return RawJobData(
            reference=reference,
            html=response.text,
            final_url=str(response.url),
            status_code=response.status_code,
            fetched_at=datetime.now(UTC),
        )

    async def normalize_job(self, raw_job: RawJobData) -> NormalizedJobData:
        tree = HTMLParser(raw_job.html)
        job_id = self._job_id(raw_job.final_url) or raw_job.reference.external_id

        # 1. Job closed status detection
        title_node = tree.css_first("h2.job-item-title") or tree.css_first(".page-description h1")
        status_nodes = tree.css(".page-description .alert, .job-status, .job-expired")
        body_text_lower = " ".join(node.text(strip=True).lower() for node in status_nodes)
        if title_node is None:
            body_text_lower = tree.body.text(strip=True).lower() if tree.body else ""
        is_closed = any(m in body_text_lower for m in _CLOSED_MARKERS) or raw_job.status_code in {
            404,
            410,
        }
        status = JobStatus.CLOSED if is_closed else JobStatus.ACTIVE
        if not is_closed and (
            title_node is None
            or tree.css_first(".page-description") is None
            or tree.css_first("#job-description") is None
        ):
            raise DelucruMdDegradedError("public response does not contain a recognizable job card")

        # 2. Canonical & Localized URLs
        canonical_url = f"{self.config.base_url}/job/{job_id}"
        localized_urls = {
            "ro": f"{self.config.base_url}/job/{job_id}",
            "ru": f"{self.config.base_url}/ru/job/{job_id}",
        }
        page_locale = "ru" if "/ru/" in raw_job.final_url else "ro"

        # 3. Title
        title = self._clean_text(title_node.text(strip=True)) if title_node else ""
        if not title:
            title_tag = tree.css_first("title")
            title = self._clean_text(title_tag.text(strip=True)) if title_tag else f"Job {job_id}"
        title = re.sub(r"\s*[-–]\s*delucru\.md$", "", title, flags=re.IGNORECASE).strip()

        # 4. Company & Company ID
        comp_inp = tree.css_first("input[name='company_id']")
        company_id = comp_inp.attributes.get("value") if comp_inp else None

        # Avoid "Top Angajator 2025" link trap
        comp_link = tree.css_first(
            "a[href*='delucru.md/company/'][title*='Loc de muncă în'], "
            "a[href*='/company/'][title*='Loc de muncă în'], "
            "a[href*='/company/'][title*='вакантная позиция']"
        )
        if not comp_link:
            for a in tree.css("a[href*='/company/']"):
                href = a.attributes.get("href") or ""
                if "topangajatori.md" not in href and "javascript:" not in href:
                    comp_link = a
                    break

        company_name: str | None = None
        employer_url: str | None = None
        if comp_link:
            comp_href = comp_link.attributes.get("href") or ""
            employer_url = self._candidate_public_url(raw_job.final_url, comp_href)
            em = comp_link.css_first("em")
            company_name = self._clean_text(
                em.text(strip=True) if em else comp_link.text(strip=True)
            )
            if not company_id and employer_url:
                m_cid = re.search(r"-(\d+)$", employer_url)
                if m_cid:
                    company_id = m_cid.group(1)

        # 5. Metadata cards
        metadata: dict[str, str] = {}
        for col in tree.css(".page-description .col-6, .page-description .col"):
            t = self._clean_text(col.text(separator=" ", strip=True))
            if ":" in t:
                k, v = t.split(":", 1)
                metadata[k.strip().lower()] = v.strip()

        # 6. Salary Parsing
        salary_raw = metadata.get("salariu") or metadata.get("зарплата")
        sal_min, sal_max, cur = self._parse_salary(salary_raw)

        # 7. Location & Districts
        loc_raw = (
            metadata.get("oraș")
            or metadata.get("oras")
            or metadata.get("город")
            or raw_job.reference.region
        )
        city, cities, districts = self._parse_location(loc_raw)

        # 8. Workplace Type
        workplace_raw = (
            metadata.get("locație") or metadata.get("locatie") or metadata.get("место работы") or ""
        )
        workplace_type = self._parse_workplace_type(workplace_raw, tree)

        # 9. Schedule & Employment Type
        sched_raw = metadata.get("program de lucru") or metadata.get("вид занятости")
        schedule, employment_type = self._parse_schedule(sched_raw)

        # 10. Experience
        exp_raw = (
            metadata.get("experiență")
            or metadata.get("experienta")
            or metadata.get("oпыт")
            or metadata.get("опыт")
        )
        req_exp, no_exp = self._parse_experience(exp_raw)

        # 11. Description, Responsibilities, Requirements
        desc_node = tree.css_first("#job-description")
        description = (
            "\n".join(
                line
                for text in desc_node.text(separator="\n", strip=True).splitlines()
                if (line := self._clean_text(text))
            )
            if desc_node
            else ""
        )
        responsibilities, requirements = self._split_sections(description)

        # 12. Zero-Click Contacts
        emails, phones, web, socials = self._extract_contacts(tree, raw_job.final_url)

        # 13. Dates
        published_at, updated_at = self._extract_dates(tree, raw_job.fetched_at)

        # 14. Application URL
        app_url = f"{self.config.base_url}/jobs/form/{job_id}"
        click_link = tree.css_first(f"a[href*='/jobs/click/{job_id}']")
        if click_link:
            click_href = click_link.attributes.get("href") or ""
            cand_click = self._candidate_public_url(
                raw_job.final_url, click_href, allow_application_action=True
            )
            if cand_click:
                app_url = cand_click

        # 15. Content Hash and Fingerprint
        hash_payload = {
            "title": title,
            "company": company_name,
            "salary_text": salary_raw,
            "salary_min": str(sal_min) if sal_min is not None else None,
            "salary_max": str(sal_max) if sal_max is not None else None,
            "currency": cur,
            "city": city,
            "cities": sorted(cities),
            "districts": sorted(districts),
            "description": description,
            "requirements": requirements,
            "responsibilities": responsibilities,
            "employer_url": employer_url,
            "company_website": web,
            "schedule": schedule,
            "employment_type": employment_type,
            "required_experience": req_exp,
            "no_experience": no_exp,
            "workplace_type": workplace_type,
            "contacts": {
                "emails": sorted(emails),
                "phones": sorted(phones),
                "application_url": app_url,
            },
            "status": status.value,
        }
        content_hash = self._hash_json(hash_payload)
        fingerprint = self._hash_json(
            {
                "company": self._fingerprint_text(company_name),
                "title": self._fingerprint_text(title),
                "city": self._fingerprint_text(city),
            }
        )

        categories_seen = self._string_list(raw_job.reference.metadata.get("categories_seen"))
        known_categories = {slug for slug, _, _ in _KNOWN_CATEGORIES}
        known_categories.update(item.external_id for item in self._category_cache or [])
        for link in tree.css(".page-description a[href*='/jobs/'], .breadcrumb a[href*='/jobs/']"):
            match = _CATEGORY_PATH_RE.match(urlsplit(link.attributes.get("href") or "").path)
            if match and match.group("category") in known_categories:
                categories_seen.append(match.group("category"))
        if raw_job.reference.category and raw_job.reference.category not in categories_seen:
            categories_seen.append(raw_job.reference.category)

        return NormalizedJobData(
            external_job_id=job_id,
            canonical_url=canonical_url,
            localized_urls=localized_urls,
            title=title,
            company=company_name,
            employer_url=employer_url,
            category=raw_job.reference.category or next(iter(sorted(set(categories_seen))), None),
            categories_seen=sorted(set(categories_seen)),
            description=description,
            responsibilities=responsibilities,
            requirements=requirements,
            salary_text=salary_raw,
            salary_min=sal_min,
            salary_max=sal_max,
            currency=cur,
            city=city,
            cities=cities,
            employment_type=employment_type,
            schedule=schedule,
            required_experience=req_exp,
            no_experience=no_exp,
            workplace_type=workplace_type,
            public_email=emails[0] if emails else None,
            public_phone=phones[0] if phones else None,
            public_emails=emails,
            public_phones=phones,
            application_url=app_url,
            page_locale=page_locale,
            published_at=published_at,
            updated_at=updated_at,
            content_hash=content_hash,
            source_fingerprint=fingerprint,
            status=status,
            raw_metadata={
                "company_id": company_id,
                "districts": districts,
                "company_website": web,
                "social_links": socials,
                "content_hash_version": _CONTENT_HASH_VERSION,
                "listing_updated_hint": raw_job.reference.updated_hint,
                "discovery_url": raw_job.reference.discovery_url,
            },
        )

    async def recheck_job(self, job: Any) -> JobRecheckResult:
        url = self._job_attribute(job, "canonical_url")
        external_id = self._job_attribute(job, "external_job_id")
        old_hash = self._job_attribute(job, "content_hash")
        if not isinstance(url, str) or not isinstance(external_id, str):
            raise DelucruMdParseError("recheck requires canonical_url and external_job_id")

        reference = RawJobReference(
            external_id=external_id,
            url=url,
            locale="ru" if "/ru/" in url else "ro",
        )
        try:
            response = await self._get_public_page(url)
        except (httpx.TimeoutException, httpx.NetworkError, DelucruMdTemporaryError) as exc:
            return JobRecheckResult(exists=None, temporary_error=str(exc))
        except (DelucruMdAccessDenied, DelucruMdDegradedError) as exc:
            return JobRecheckResult(exists=None, temporary_error=str(exc), adapter_degraded=True)

        if response.status_code in {404, 410}:
            return JobRecheckResult(exists=False, explicitly_closed=response.status_code == 410)
        if response.status_code in {403, 429} or response.status_code >= 500:
            return JobRecheckResult(
                exists=None,
                temporary_error=f"Delucru.md returned HTTP {response.status_code}",
                adapter_degraded=response.status_code in {403, 429},
            )

        try:
            raw = RawJobData(
                reference=reference,
                html=response.text,
                final_url=str(response.url),
                status_code=response.status_code,
                fetched_at=datetime.now(UTC),
            )
            normalized = await self.normalize_job(raw)
        except (DelucruMdParseError, DelucruMdDegradedError) as exc:
            return JobRecheckResult(exists=None, temporary_error=str(exc), adapter_degraded=True)

        return JobRecheckResult(
            exists=True,
            explicitly_closed=normalized.status == JobStatus.CLOSED,
            changed=not isinstance(old_hash, str) or normalized.content_hash != old_hash,
            normalized_job=normalized,
        )

    # Internal helpers

    async def _iterate_scan(
        self,
        checkpoint: ScanCheckpoint | None,
        *,
        incremental: bool,
    ) -> AsyncIterator[RawJobReference]:
        await self._ensure_access()
        state = checkpoint or ScanCheckpoint()
        self.last_checkpoint = state
        self._references_by_id = {}
        seen_ids = set(state.yielded_external_ids)
        known_ids = set(self._string_list(state.adapter_state.get("known_external_ids")))
        known_hints_raw = state.adapter_state.get("known_updated_hints", {})
        known_hints = known_hints_raw if isinstance(known_hints_raw, dict) else {}
        checks_raw = state.adapter_state.get("known_last_checked_at", {})
        checks = checks_raw if isinstance(checks_raw, dict) else {}
        refresh_ids = set(self._string_list(state.adapter_state.get("detail_refresh_selected_ids")))
        scan_time = datetime.now(UTC)
        state.adapter_state["scan_incomplete"] = False

        def reference_state(ref: RawJobReference) -> tuple[bool, bool, bool]:
            if not incremental or ref.external_id not in known_ids:
                return False, False, False
            if ref.updated_hint and known_hints.get(ref.external_id) != ref.updated_hint:
                return False, False, False
            checked_at = None
            checked = checks.get(ref.external_id)
            if isinstance(checked, str):
                with contextlib.suppress(ValueError):
                    checked_at = datetime.fromisoformat(checked.replace("Z", "+00:00"))
                    if checked_at.tzinfo is None:
                        checked_at = checked_at.replace(tzinfo=UTC)
            jitter = self.config.incremental_refresh_jitter_hours * 3600
            digest = hashlib.sha256(ref.external_id.encode()).digest()
            offset = int.from_bytes(digest[:8], "big") % (2 * jitter + 1) - jitter
            max_age = timedelta(
                seconds=max(
                    3600, self.config.incremental_known_detail_refresh_hours * 3600 + offset
                )
            )
            if checked_at is not None and timedelta(0) <= scan_time - checked_at <= max_age:
                return True, False, False
            if (
                ref.external_id in refresh_ids
                or len(refresh_ids) < self.config.incremental_detail_refresh_budget
            ):
                refresh_ids.add(ref.external_id)
                state.adapter_state["detail_refresh_selected_ids"] = sorted(refresh_ids)
                return False, True, False
            return True, True, True

        saved_entries = state.adapter_state.get("scan_entrypoints")
        if isinstance(saved_entries, list):
            entrypoints = saved_entries
        else:
            entrypoints = await self._scan_entrypoints(incremental=incremental)
            state.adapter_state["scan_entrypoints"] = entrypoints
        max_pages = (
            self.config.incremental_max_pages_per_entrypoint
            if incremental
            else self.config.max_pages_per_entrypoint
        )

        start_index = min(state.entrypoint_index, len(entrypoints))
        for index in range(start_index, len(entrypoints)):
            unchanged_run = 0
            entry = entrypoints[index]
            entry_url = entry["url"]
            if not isinstance(entry_url, str) or entry_url in state.completed_entrypoints:
                continue

            current_url: str | None = (
                state.page_url if index == start_index and state.page_url else entry_url
            )
            visited_pages: set[str] = set()
            pages = 0

            while current_url and pages < max_pages:
                if current_url in visited_pages:
                    raise DelucruMdDegradedError("pagination loop detected")
                visited_pages.add(current_url)
                state.entrypoint_index = index
                state.page_url = current_url

                response = await self._get_public_page(current_url)
                if response.status_code == 404:
                    current_url = None
                    break
                self._require_success(response, current_url)
                self._validate_listing(response.text)
                pages += 1

                references = self._references_from_listing(
                    response.text,
                    str(response.url),
                    category=entry.get("category"),
                    region=entry.get("region"),
                )

                for ref in references:
                    unchanged, due, deferred = reference_state(ref)
                    unchanged_run = unchanged_run + 1 if unchanged and not due else 0
                    existing = self._references_by_id.get(ref.external_id)
                    if existing is not None:
                        ref.category = existing.category or ref.category
                        ref.metadata["categories_seen"] = sorted(
                            set(
                                self._string_list(existing.metadata.get("categories_seen"))
                                + self._string_list(ref.metadata.get("categories_seen"))
                            )
                        )
                        ref.metadata["localized_urls"] = {
                            **existing.metadata.get("localized_urls", {}),
                            **ref.metadata.get("localized_urls", {}),
                        }
                    self._references_by_id[ref.external_id] = ref
                    duplicate = ref.external_id in seen_ids
                    if not duplicate:
                        seen_ids.add(ref.external_id)
                        state.yielded_external_ids.append(ref.external_id)
                    yielded = ref.model_copy(deep=True)
                    yielded.metadata.update(
                        {
                            "known_unchanged": unchanged,
                            "detail_refresh_due": due,
                            "detail_refresh_deferred": deferred,
                            "duplicate_reference": duplicate,
                            "scan_checkpoint": state.model_dump(
                                mode="json",
                                exclude={
                                    "adapter_state": {
                                        "known_external_ids",
                                        "known_updated_hints",
                                        "known_last_checked_at",
                                    }
                                },
                            ),
                        }
                    )
                    yield yielded

                    if incremental and unchanged_run >= self.config.known_unchanged_stop_threshold:
                        current_url = None
                        break
                else:
                    current_url = self._next_page_url(
                        response.text, str(response.url), visited_pages
                    )
                    continue
                break

            if current_url is not None and pages >= max_pages:
                state.page_url = current_url
                state.adapter_state["scan_incomplete"] = True
                return
            if entry_url not in state.completed_entrypoints:
                state.completed_entrypoints.append(entry_url)
            state.entrypoint_index = index + 1
            state.page_url = None

    async def _scan_entrypoints(self, *, incremental: bool) -> list[dict[str, str | None]]:
        entrypoints: list[dict[str, str | None]] = []
        locales = await self.discover_locales()
        hot = {slug.casefold() for slug in self.config.incremental_category_slugs}
        for cat in await self.discover_categories():
            if not incremental or cat.external_id.casefold() in hot:
                entrypoints.append({"url": cat.url, "category": cat.external_id, "region": None})
        if incremental:
            # A missing locale link in the directory must not drop a selected category.
            listed_urls = {entry["url"] for entry in entrypoints}
            for locale in locales:
                prefix = "" if locale.code == "ro" else f"/{locale.code}"
                for slug in sorted(hot):
                    url = f"{self.config.base_url}{prefix}/jobs/{slug}"
                    if url not in listed_urls:
                        entrypoints.append({"url": url, "category": slug, "region": None})
        if not incremental:
            # General listings for each locale
            for loc in locales:
                prefix = "" if loc.code == "ro" else f"/{loc.code}"
                entrypoints.append(
                    {
                        "url": f"{self.config.base_url}{prefix}/jobs",
                        "category": None,
                        "region": None,
                    }
                )
            for region in await self.discover_regions():
                entrypoints.append(
                    {"url": region.url, "category": None, "region": region.external_id}
                )

        deduped: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for e in entrypoints:
            u = e["url"]
            if u and u not in seen:
                seen.add(u)
                deduped.append(e)
        if len(deduped) > self.config.max_discovered_entrypoints:
            raise DelucruMdParseError("discovered entrypoints exceed the configured scan budget")
        return deduped

    def _references_from_listing(
        self, html: str, page_url: str, *, category: str | None, region: str | None = None
    ) -> list[RawJobReference]:
        tree = HTMLParser(html)
        refs: dict[str, RawJobReference] = {}
        for a in tree.css("a[href*='/job/'], a.link-job"):
            href = a.attributes.get("href") or ""
            cand = self._candidate_public_url(page_url, href)
            if not cand:
                continue
            job_id = self._job_id(cand)
            if not job_id:
                continue

            # Updated date hint from link title or card
            title_attr = a.attributes.get("title") or ""
            m_date = re.search(r"(\d{2}\.\d{2}\.\d{4})", title_attr)
            updated_hint = m_date.group(1) if m_date else None
            locale = "ru" if "/ru/" in cand else "ro"

            refs[job_id] = RawJobReference(
                external_id=job_id,
                url=cand,
                locale=locale,
                category=category,
                region=region,
                discovery_url=page_url,
                updated_hint=updated_hint,
                metadata={
                    "categories_seen": [category] if category else [],
                    "localized_urls": {locale: cand},
                },
            )
        return list(refs.values())

    def _next_page_url(self, html: str, page_url: str, visited: set[str]) -> str | None:
        tree = HTMLParser(html)
        current_query = dict(parse_qsl(urlsplit(page_url).query))
        try:
            current_page = int(current_query.get("page", "1"))
        except ValueError as exc:
            raise DelucruMdParseError("pagination page number is invalid") from exc
        for link in tree.css("a[rel~='next'][href], a.page-link, ul.pagination a"):
            parent = link.parent
            classes = (link.attributes.get("class") or "").split()
            parent_classes = (parent.attributes.get("class") or "").split() if parent else []
            if (
                "disabled" in classes
                or "disabled" in parent_classes
                or link.attributes.get("aria-disabled") == "true"
            ):
                continue
            text = self._clean_text(link.text(separator=" ", strip=True)).casefold()
            is_next = "next" in (link.attributes.get("rel") or "").split() or text in {
                "următoarea",
                "urmatoarea",
                "следующая",
                "next",
                ">",
                "»",
                "›",
            }
            is_next_number = text.isdecimal() and int(text) == current_page + 1
            if not (is_next or is_next_number):
                continue
            href = link.attributes.get("href")
            if not href or href.startswith("#"):
                continue
            candidate = self._candidate_public_url(page_url, href)
            if candidate is None:
                continue
            if candidate in visited:
                raise DelucruMdDegradedError("pagination loop in listing")
            if is_next_number:
                candidate_parts = urlsplit(candidate)
                candidate_query = dict(parse_qsl(candidate_parts.query))
                if candidate_parts.path != urlsplit(page_url).path or candidate_query.get(
                    "page"
                ) != str(current_page + 1):
                    continue
            return candidate
        # A current-page marker does not imply another page exists. Never invent
        # page=N+1 after the last page of a finite category.
        return None

    async def _ensure_access(self) -> None:
        policy = self._access_result or await self.check_access_policy()
        if not policy.allowed:
            raise DelucruMdAccessDenied(policy.reason)

    async def _get_public_page(self, url: str) -> httpx.Response:
        await self._ensure_access()
        candidate = self._require_public_url(url)
        response = await self._http.get(candidate)
        self._require_public_url(str(response.url))
        if response.status_code == 200:
            tree = HTMLParser(response.text)
            title = tree.css_first("title")
            text = title.text(strip=True).casefold() if title is not None else ""
            if any(
                marker in text
                for marker in ("please wait", "just a moment", "captcha", "access denied")
            ):
                raise DelucruMdDegradedError(
                    "source returned an access challenge instead of public content"
                )
        return response

    def _validate_listing(self, html: str) -> None:
        tree = HTMLParser(html)
        if tree.css_first("a[href*='/job/'], .empty-results") is not None:
            return
        empty_messages = (
            "pentru această căutare nu au fost găsite locuri de muncă.",
            "по данному запросу не было найдено доступных вакансий.",
        )
        if any(
            self._clean_text(node.text(separator=" ", strip=True))
            .casefold()
            .startswith(empty_messages)
            for node in tree.css(".alert.alert-info")
        ):
            return
        raise DelucruMdDegradedError("public response does not contain a recognizable jobs listing")

    @staticmethod
    def _require_success(response: httpx.Response, url: str) -> None:
        if response.status_code in {403, 429}:
            raise DelucruMdDegradedError(
                f"Delucru.md rate limited or forbidden (HTTP {response.status_code}) for {url}"
            )
        if response.status_code >= 500:
            raise DelucruMdTemporaryError(
                f"Delucru.md server error (HTTP {response.status_code}) for {url}"
            )
        if response.status_code >= 400 and response.status_code not in {404, 410}:
            raise DelucruMdParseError(
                f"Delucru.md unexpected client error (HTTP {response.status_code}) for {url}"
            )

    def _require_public_url(self, url: str, *, allow_application_action: bool = False) -> str:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or hostname not in {"delucru.md", "www.delucru.md"}
            or parsed.port not in {None, 443}
        ):
            raise DelucruMdAccessDenied("URL is outside the HTTPS Delucru.md source allowlist")
        path = parsed.path or "/"
        lowered = unquote(path).casefold()
        if any(part in lowered for part in _INTERNAL_ACTION_PARTS) and (
            not allow_application_action
            or not re.fullmatch(r"/(?:ru/|ro/)?jobs/(?:form|click)/\d+/?", lowered)
        ):
            raise DelucruMdAccessDenied(
                "internal application and authentication actions are not crawlable"
            )
        return urlunsplit(("https", parsed.netloc, path, parsed.query, ""))

    def _candidate_public_url(
        self, current_url: str, href: str, *, allow_application_action: bool = False
    ) -> str | None:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            return None
        try:
            return self._require_public_url(
                urljoin(current_url, href.strip()),
                allow_application_action=allow_application_action,
            )
        except (ValueError, DelucruMdAccessDenied):
            return None

    @staticmethod
    def _job_id(url: str) -> str | None:
        match = _JOB_PATH_RE.match(urlsplit(url).path)
        return match.group("job_id") if match else None

    @staticmethod
    def _clean_text(value: str | None) -> str:
        if not value:
            return ""
        return re.sub(r"\s+", " ", value.replace("\u200b", " ").replace("\u00a0", " ")).strip()

    @staticmethod
    def _fingerprint_text(value: str | None) -> str:
        return re.sub(r"[\W_]+", " ", (value or "").casefold()).strip()

    @staticmethod
    def _hash_json(value: dict[str, Any]) -> str:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    @staticmethod
    def _job_attribute(job: Any, name: str) -> Any:
        if isinstance(job, dict):
            return job.get(name)
        return getattr(job, name, None)

    @staticmethod
    def _parse_salary(value: str | None) -> tuple[Decimal | None, Decimal | None, str | None]:
        if not value:
            return None, None, None
        folded = value.casefold()
        if "negociabil" in folded or "договорная" in folded:
            return None, None, None

        currency = None
        if "eur" in folded or "€" in value:
            currency = "EUR"
        elif "usd" in folded or "$" in value:
            currency = "USD"
        elif "mdl" in folded or "lei" in folded or "лей" in folded:
            currency = "MDL"

        norm_sal = value.replace("\u00a0", " ").replace(",", "").strip()
        m_range = re.search(
            r"(?:de\s+la|от)?\s*(\d+(?:\s+\d+)?)\s*(?:până\s+la|до|-)\s*(\d+(?:\s+\d+)?)",
            norm_sal,
            re.IGNORECASE,
        )
        m_from = re.search(r"(?:de\s+la|от)\s*(\d+(?:\s+\d+)?)", norm_sal, re.IGNORECASE)
        m_to = re.search(r"(?:până\s+la|до)\s*(\d+(?:\s+\d+)?)", norm_sal, re.IGNORECASE)
        m_exact = re.search(r"(\d+(?:\s+\d+)?)", norm_sal)

        try:
            if m_range:
                val1 = Decimal(m_range.group(1).replace(" ", ""))
                val2 = Decimal(m_range.group(2).replace(" ", ""))
                return min(val1, val2), max(val1, val2), currency
            if m_from:
                return Decimal(m_from.group(1).replace(" ", "")), None, currency
            if m_to:
                return None, Decimal(m_to.group(1).replace(" ", "")), currency
            if m_exact:
                val = Decimal(m_exact.group(1).replace(" ", ""))
                return val, val, currency
        except (InvalidOperation, ValueError):
            pass
        return None, None, currency

    @staticmethod
    def _parse_location(value: str | None) -> tuple[str | None, list[str], list[str]]:
        if not value:
            return None, [], []
        cities: list[str] = []
        districts: list[str] = []
        tokens = [t.strip() for t in value.split(",") if t.strip()]

        for tok in tokens:
            low = tok.lower()
            if low in _CHISINAU_DISTRICTS:
                districts.append(_CHISINAU_DISTRICTS[low])
                if "Chișinău" not in cities:
                    cities.append("Chișinău")
            elif low in _CITY_CANONICAL_MAP:
                canon = _CITY_CANONICAL_MAP[low]
                if canon not in cities:
                    cities.append(canon)
            else:
                if tok not in cities:
                    cities.append(tok)

        city = cities[0] if cities else None
        return city, cities, districts

    @staticmethod
    def _parse_workplace_type(
        value: str, tree: HTMLParser
    ) -> Literal["remote", "hybrid", "onsite"] | None:
        low = value.lower()
        if any(k in low for k in ["remote", "distanță", "distanta", "удален"]):
            return "remote"
        if any(k in low for k in ["hibrid", "hybrid", "гибрид"]):
            return "hybrid"
        if any(k in low for k in ["locația", "locatia", "местоположение", "onsite", "sediu"]):
            return "onsite"
        for badge in tree.css(".row.gx-3 [class*='col']"):
            b_text = badge.text(strip=True).lower()
            if "remote" in b_text or "distanță" in b_text:
                return "remote"
        return None

    @staticmethod
    def _parse_schedule(value: str | None) -> tuple[str | None, str | None]:
        if not value:
            return None, None
        sched = DelucruMdAdapter._clean_text(value)
        low = sched.lower()
        emp_type = None
        if "full-time" in low or "полная" in low:
            emp_type = "full-time"
        elif "part-time" in low or "частичная" in low:
            emp_type = "part-time"
        elif "proiect" in low or "sezonier" in low or "временная" in low:
            emp_type = "temporary"
        return sched, emp_type

    @staticmethod
    def _parse_experience(value: str | None) -> tuple[str | None, bool | None]:
        if not value:
            return None, None
        exp = DelucruMdAdapter._clean_text(value)
        low = exp.lower()
        no_exp = "fără experiență" in low or "fara experienta" in low or "без опыта" in low
        return exp, no_exp

    @staticmethod
    def _split_sections(description: str) -> tuple[str | None, str | None]:
        responsibilities = None
        requirements = None
        resp_pattern = (
            r"(?:responsabilitățile candidatului|responsabilități|key responsibilities|"
            r"ce vei face|workflow description|обязанности)[\s:]+(.*?)"
            r"(?=(?:cerințe|ce ne dorim|requirements|we look for|требования|"
            r"oferim|beneficii|oferta|condiții|условия|$))"
        )
        resp_m = re.search(resp_pattern, description, re.IGNORECASE | re.DOTALL)
        if resp_m:
            responsibilities = resp_m.group(1).strip()

        req_pattern = (
            r"(?:cerințe față de candidat|cerințe|requirements|ce ne dorim de la tine|"
            r"we look for|требования)[\s:]+(.*?)"
            r"(?=(?:responsabilități|key responsibilities|oferim|"
            r"beneficii|oferta|ce oferim|condiții|условия|$))"
        )
        req_m = re.search(req_pattern, description, re.IGNORECASE | re.DOTALL)
        if req_m:
            requirements = req_m.group(1).strip()
        return responsibilities, requirements

    def _extract_contacts(
        self, tree: HTMLParser, page_url: str
    ) -> tuple[list[str], list[str], str | None, list[str]]:
        scope = tree.css_first("div.page-description")
        if scope is None:
            return [], [], None, []
        emails: list[str] = []
        phones: list[str] = []
        website: str | None = None
        socials: list[str] = []

        # Emails: mailto links
        for a in scope.css("a[href^='mailto:']"):
            href = a.attributes.get("href") or ""
            val = unquote(href.replace("mailto:", "").split("?", 1)[0]).strip().lower()
            if val and val not in _DELUCRU_SERVICE_EMAILS and _PUBLIC_EMAIL_RE.fullmatch(val):
                emails.append(val)

        # Exact unmasked values from copy buttons
        for el in scope.css("[data-copy-value]"):
            val = (el.attributes.get("data-copy-value") or "").strip()
            if _PUBLIC_EMAIL_RE.fullmatch(val) and val.lower() not in _DELUCRU_SERVICE_EMAILS:
                emails.append(val.lower())
            else:
                clean_phone = re.sub(r"[^\d+]", "", val)
                norm_phone = normalize_e164(clean_phone, region="MD")
                if norm_phone and norm_phone not in _DELUCRU_SERVICE_PHONES:
                    phones.append(norm_phone)

        for span in scope.css("[data-contact-type='email'], .job-contact-mask-value"):
            t = span.text(separator=" ", strip=True).lower()
            t = re.sub(r"\b(?:copiat|скопировано)\b", "", t).strip()
            m = _PUBLIC_EMAIL_RE.search(t)
            if m:
                val = m.group(0).lower()
                if val not in _DELUCRU_SERVICE_EMAILS:
                    emails.append(val)

        # Phones: tel links and unmasked spans
        for a in scope.css("a[href^='tel:']"):
            href = a.attributes.get("href") or ""
            val = unquote(href.replace("tel:", "").split("?", 1)[0]).strip()
            clean_val = re.sub(r"[^\d+]", "", val)
            norm = normalize_e164(clean_val, region="MD")
            if norm and norm not in _DELUCRU_SERVICE_PHONES:
                phones.append(norm)

        for span in scope.css("[data-contact-type='phone'], .job-contact-mask-value"):
            t = span.text(separator=" ", strip=True)
            t = re.sub(r"\b(?:copiat|скопировано)\b", "", t, flags=re.IGNORECASE).strip()
            m_phone = re.search(r"(?:\+?\d[\d\s().-]{6,14}\d)", t)
            if m_phone:
                clean_val = re.sub(r"[^\d+]", "", m_phone.group(0))
                norm = normalize_e164(clean_val, region="MD")
                if norm and norm not in _DELUCRU_SERVICE_PHONES:
                    phones.append(norm)

        # Website and Socials
        contacts_el = tree.css_first("#contacts")
        if contacts_el:
            for a in contacts_el.css("a.link-primary[href]"):
                href = a.attributes.get("href") or ""
                if (
                    href
                    and not href.startswith(("mailto:", "tel:", "#", "javascript:"))
                    and "delucru.md" not in href
                ):
                    clean_web = re.sub(r"^http://https://", "https://", href)
                    website = clean_web
                    break

            for a in contacts_el.css(".social-icons-job a[href]"):
                href = a.attributes.get("href") or ""
                if href and "delucru.md" not in href:
                    clean_soc = re.sub(r"^http://https://", "https://", href)
                    socials.append(clean_soc)

        return (
            list(dict.fromkeys(emails)),
            list(dict.fromkeys(phones)),
            website,
            list(dict.fromkeys(socials)),
        )

    @staticmethod
    def _extract_dates(
        tree: HTMLParser, fetched_at: datetime
    ) -> tuple[datetime | None, datetime | None]:
        updated_at: datetime | None = None
        # Title link e.g. "poziție vacantă de la 03.10.2026"
        t_link = tree.css_first(
            "a[title*='poziție vacantă de la'], a[title*='вакантная позиция с']"
        )
        if t_link:
            title_text = t_link.attributes.get("title") or ""
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", title_text)
            if m:
                with contextlib.suppress(ValueError):
                    updated_at = datetime(
                        int(m.group(3)),
                        int(m.group(2)),
                        int(m.group(1)),
                        tzinfo=ZoneInfo("Europe/Chisinau"),
                    )

        if not updated_at:
            date_el = tree.css_first(".page-item-date")
            if date_el:
                dt_str = date_el.text(strip=True).lower()
                if "azi" in dt_str or "сегодня" in dt_str:
                    local_time = fetched_at.astimezone(ZoneInfo("Europe/Chisinau"))
                    updated_at = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
                elif "ieri" in dt_str or "вчера" in dt_str:
                    local_time = fetched_at.astimezone(ZoneInfo("Europe/Chisinau"))
                    updated_at = (local_time - timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )

        published_at = updated_at
        return published_at, updated_at
