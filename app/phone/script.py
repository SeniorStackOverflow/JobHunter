from __future__ import annotations

# Disclosure + one simple question. The caller gets a real RX window after this
# block; JobHunter must not immediately fire another TTS block over their reply.
SCRIPT_GREETING: tuple[str, ...] = (
    "Здравствуйте, это автоматизированный помощник Андрея Гомонова. Вы звоните по поводу работы?",
)

SCRIPT_FIRST_RESPONSE_RETRY: str = (
    "Вы меня слышите? Если звоните по поводу работы, пожалуйста, скажите, что Андрей должен знать."
)

SCRIPT_DETAILS_PROMPT: str = (
    "Спасибо, я записываю. Расскажите, пожалуйста, детали, которые Андрей должен знать."
)

SCRIPT_CLOSING: str = "Спасибо, я записал. Андрей свяжется с вами. Всего доброго."

SCRIPT_CLOSING_FOLLOW_UP: str = "Спасибо, я записал и передам Андрею. Всего доброго."

SCRIPT_CLOSING_NO_RESPONSE: str = (
    "Похоже, я вас не услышал. Андрей при необходимости свяжется с вами. Всего доброго."
)

SCRIPT_CLOSING_SMS: str = (
    "Спасибо. Чтобы избежать ошибки в дате и времени, пожалуйста, отправьте данные "
    "собеседования SMS на этот номер. Андрей свяжется с вами. Всего доброго."
)

SCRIPT_CLOSING_INTERRUPTED: str = (
    "Извините, мне нужно прервать разговор. Андрей свяжется с вами. Всего доброго."
)
