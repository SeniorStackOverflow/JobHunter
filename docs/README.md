# Документация JobHunter

Актуализировано 2026-09-30 по коду, Compose и read-only проверке PROD.
Название Python-пакета и CLI остаётся `job-agent`; название продукта — JobHunter.

## Актуальные инструкции

| Задача | Документ |
| --- | --- |
| Устройство системы и границы сервисов | [Архитектура](architecture.md) |
| DEV, PROD, версии и безопасные проверки | [Окружения](environments.md) |
| Установка, обновление, откат и очистка Docker | [Развёртывание](deployment.md) |
| Ежедневные проверки, инциденты, backup/restore | [Эксплуатация](operations.md) |
| Панель, аккаунты, приглашения и права | [Аккаунты и панель](accounts-panel.md) |
| Статусы, задачи и архив уведомлений | [Уведомления](panel-notifications.md) |
| Вход Google и сохранение сессии | [Сессии](google-oauth-sessions.md) |
| Отдельное подключение Gmail, доставка и reconnect | [Gmail OAuth](gmail-oauth.md) |
| Условия отправки | [Политика автоотправки](auto-send-policy.md) |
| Добор минимума в разные компании | [Добор минимума](daily-minimum-catchup.md) |
| Контакты и история работодателя | [Память работодателей](employer-relationships.md) |
| Явные решения, подсказки и shadow-модель | [Обучение review](review-learning.md) |
| Удалённый привилегированный интерфейс | [MCP](mcp.md) |
| Контракт и жизненный цикл источника | [Crawler](crawlers.md), [добавление источника](adding-source.md) |
| Текущий транспорт Rabota.md | [Rabota.md](sources/rabota-md.md) |
| Аварийный браузерный профиль и proxy reserve | [Rabota emergency](operations/rabota-browser-emergency.md) |
| Телефонный канал, подтверждение фактов и обслуживание | [Телефон](phone-agent.md) |
| Контроли доступа, секреты и доверительные границы | [Безопасность](security.md), [модель угроз](threat-model.md) |

## Исторические материалы

[Планы и спецификации](superpowers/README.md) сохраняют исходные решения,
предложения и шаги реализации. Они не являются актуальными командами эксплуатации
и не подтверждают, что каждая предложенная функция реализована.

- [Исследование недобора дневного минимума 27–28 сентября](2026-09-29-daily-minimum-investigation.md).
- [Ошибки LLM, score 0 и sent/submitted 30 сентября](2026-09-30-llm-delivery-investigation.md).
- [Первоначальная архитектура телефонного агента](phonegate-call-agent-architecture.md).
- [Изменения turn-taking 2026-09-14](phone-turn-taking-2026-09-14.md).
- [DEV acceptance Phase 2b](operations/phone-phase-2b-dev-acceptance.md).
- [Исследование HTTP/WAF](sources/rabota-md-http.md).
- [Исследование pure-Python solver](sources/rabota-md-pure-python.md).

Даты, результаты live-проверок, старые SHA и migration head в этих материалах
относятся к указанному моменту, а не к текущему окружению. При расхождении текущий
код, конфигурация и актуальные инструкции выше имеют приоритет.
