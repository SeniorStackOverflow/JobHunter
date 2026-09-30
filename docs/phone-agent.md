# Телефонный агент: текущая реализация

Актуализировано 2026-09-30 по `app/phone`, `app/admin/phone_routes.py`,
`app/scheduler/celery_app.py` и production Compose. Исходный большой design сохранён
в [архитектурном архиве](phonegate-call-agent-architecture.md).

## Граница компонентов

PhoneGate владеет GSM, звонком, RX/ASR и TTS/TX. JobHunter отвечает за корреляцию
с профилем/откликом, детерминированный сценарий, допустимые действия, подтверждение
фактов, историю и уведомления. LLM предлагает структурированные данные; он не
управляет PhoneGate напрямую и не подтверждает факты кандидата.

`call-agent` запускает `app.phone.agent`, хранит singleton lease и получает события
PhoneGate. `app.phone.ingest` сохраняет сессии и transcript; `orchestrator`/`script`
выполняют сценарий, `policy` ограничивает автоответ и разрешённые действия.
`critical`, `verification`, `facts` проверяют критичные сведения; `sms` и
`reconciliation` связывают SMS/evidence без превращения сырого ASR в истину.
`summary` формирует результат, `telegram` и `notification_state` доставляют
privacy-aware post-call уведомления при включённой настройке.

Фоновые задачи в `phone` queue обслуживает PROD `control-worker`: завершение
pending calls каждые 120 секунд, доставка уведомлений каждые 30 секунд, pruning
phone evidence в 03:40, SMS polling/reconciliation с конфигурируемым интервалом.
Сбой телефона не должен останавливать crawler, matching или Gmail.

## Панель и подтверждение

Оператор открывает `/admin?view=calls`. Там находятся здоровье канала, автоответ,
управление текущим звонком, фильтры истории, transcript, технические данные и
ручное исправление/подтверждение фактов. Обычный `/app` эти данные не получает.

Очередь review и уведомление в шапке используют одинаковое условие:
`needs_review=true` или `verification_status=needs_review`. После успешного ручного
подтверждения задача исчезает. Сам по себе старый `summary_state=failed` не
возвращает уже выполненную задачу; техническая ошибка остаётся в истории.

Работа с активным звонком имеет внешний эффект. Рутинная read-only диагностика
ограничивается состоянием контейнеров, health и сохранённой историей; не запускайте
TTS, hangup, mute или реальный GSM звонок ради проверки документации.

## Turn-taking и evidence

После соединения агент произносит одно раскрытие роли и простой вопрос, затем
реально слушает RX. Silence windows по умолчанию 4,5 секунды; активные VAD/ASR
продлевают ожидание. Remote hangup — отдельное состояние `remote_ended`;
`aborted_error` предназначен для технических сбоев. Generation-scoped call ID
помогает не смешивать поздний ASR с новым звонком. Детали изменения и отчётных
метрик — [turn-taking 2026-09-14](phone-turn-taking-2026-09-14.md).

Durable volume `phone_evidence` монтируется в writable phone processes и read-only
API. Evidence выдаётся через авторизованный код; это не публичная статика. Telegram
выключен по умолчанию и не является источником подтверждённых фактов. Retry/crash
может оставить неустранимую неоднозначность доставки стороннего уведомления.

## DEV и PROD

`PHONE_AGENT_ENABLED` и `PHONE_AUTO_ANSWER_ENABLED` — разные настройки. Health
процесса не доказывает работоспособность физического GSM/RX/TX.
Live acceptance выполняется отдельно после разрешения оператора; обычные тесты
используют fixture/fake transport и не отправляют SMS, письма или звонки.

PROD wrapper добавляет PhoneGate overlay по marker
`/etc/jobhunter/phone-agent-enabled`. Root-owned токен монтируется только в `api`,
`control-worker` и `call-agent`; не выводите его в env/logs. Первичная активация —
отдельная операторская процедура из [deployment.md](deployment.md).

JobHunter DEV не меняет PhoneGate `.env`, не ротирует её credentials и не
перезапускает `phonegate.service`. Для live DEV нужен токен, отдельно переданный
оператором с явным разрешением. Протоколы старых acceptance runs —
[исторические свидетельства](operations/phone-phase-2b-dev-acceptance.md), а не
разрешение повторять их против PROD.
