"""i18n: uk (за замовчуванням) і en.

Джерело рядків — самі шаблони й вибрані Python-рядки (title/label/помилки, а
не логи чи коментарі). `uk.po` — тотожний перекладу "сам у себе": оригінальний
текст системи вже українською, тож це формальність, яка потрібна лише щоб
`pybabel compile` дав однакову для обох мов структуру каталогу.

Мова визначається на кожен запит окремо (query `?lang=` або кука `lang`),
без сесії й без бази — це UI-переключення, а не профіль студента.
"""
from __future__ import annotations

import gettext as gettext_module
from pathlib import Path

LOCALES = ("uk", "en")
DEFAULT_LOCALE = "uk"
COOKIE_NAME = "lang"

_LOCALE_DIR = Path(__file__).parent / "locale"

_cache: dict[str, gettext_module.NullTranslations] = {}


def translations(locale: str) -> gettext_module.NullTranslations:
    """Компільовані .mo кешуються в процесі — це статичний файл на диску,
    перечитувати його на кожен запит нема сенсу."""
    if locale not in _cache:
        try:
            _cache[locale] = gettext_module.translation(
                "messages", localedir=str(_LOCALE_DIR), languages=[locale])
        except FileNotFoundError:
            # uk без .mo — NullTranslations віддає msgid як є, а це і є
            # вихідний українською текст.
            _cache[locale] = gettext_module.NullTranslations()
    return _cache[locale]


def locale_of(request) -> str:
    return pick_locale(request.query_params.get("lang"), request.cookies.get(COOKIE_NAME))


def translator(request):
    """Перекладач, привʼязаний до мови ЦЬОГО запиту — для Python-рядків
    (title/label/помилка), які не йдуть через Jinja {% trans %}. Виклик:
    `t = i18n.translator(request); t("текст")`. Аргумент — рядок першим, як
    очікує стандартний екстрактор `pybabel extract` для ключового слова `_`;
    саме тому це не просто `_(request, message)`."""
    return translations(locale_of(request)).gettext


def pick_locale(query_lang: str | None, cookie_lang: str | None) -> str:
    """Query має пріоритет над кукою: посилання з `?lang=en` має спрацювати
    негайно, а не лише після того, як кука перезапишеться і сторінку
    перезавантажать удруге."""
    if query_lang in LOCALES:
        return query_lang
    if cookie_lang in LOCALES:
        return cookie_lang
    return DEFAULT_LOCALE
