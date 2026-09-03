from __future__ import annotations

# Fixed opening blocks, spoken in order via POST /api/call/speak. Short blocks so
# the caller gets a listening gap between them (half-duplex, spec §4.3) and so
# Piper synthesis and GSM downlink quality stay high (spec §4.2).
SCRIPT_GREETING: tuple[str, ...] = (
    "Здравствуйте. Если вы звоните по поводу вакансии или собеседования с Андреем — "
    "вы позвонили по адресу.",
    "Я — голосовой ассистент Андрея и помогаю согласовать собеседования от его имени.",
    "Важные дату, время и адрес я обязательно уточню и запишу.",
    "Подскажите, пожалуйста, по какой вакансии вы звоните?",
)

SCRIPT_CLOSING: str = "Спасибо, я записал. Андрей свяжется с вами. Всего доброго."

SCRIPT_CLOSING_INTERRUPTED: str = (
    "Извините, мне нужно прервать разговор. Андрей свяжется с вами. Всего доброго."
)
