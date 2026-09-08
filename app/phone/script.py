from __future__ import annotations

# Fixed opening blocks, spoken in order via POST /api/call/speak. Short blocks so
# the caller gets a listening gap between them (half-duplex, spec §4.3) and so
# Piper synthesis and GSM downlink quality stay high (spec §4.2).
SCRIPT_GREETING: tuple[str, ...] = (
    "Здравствуйте. Я голосовой ассистент Андрея и помогаю от его имени согласовать собеседования.",
    "Назовите, пожалуйста, вакансию, дату, время, адрес и часовой пояс.",
)

SCRIPT_CLOSING: str = "Спасибо, я записал. Андрей свяжется с вами. Всего доброго."

SCRIPT_CLOSING_SMS: str = (
    "Спасибо. Чтобы избежать ошибки в дате и времени, пожалуйста, отправьте данные "
    "собеседования SMS на этот номер. Андрей свяжется с вами. Всего доброго."
)

SCRIPT_CLOSING_INTERRUPTED: str = (
    "Извините, мне нужно прервать разговор. Андрей свяжется с вами. Всего доброго."
)
