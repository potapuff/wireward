"""Wireward · веб-фронт.

Фаза 1: вхід через OIDC, провізіонінг персональної бази, кабінет студента.
Адмінка — фаза 4; тут лише мінімальний список із перевіркою прав.
"""
from __future__ import annotations

import asyncio
import http.client
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.errors import UniqueViolation
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (activity, admin, i18n, indicators, labs, oidc, provisioning, quota,
               ratelimit, students, surveys)
from .config import optional_secret, settings
from .credentials import hash_token, new_db_token
from .db import close_pools, fetchall, fetchone, open_pools
from .security import clear_session, consteq, csrf_ok, read_session, write_session

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("observer")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_null_translations(newstyle=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await open_pools()
    if settings.dev_login:
        log.warning("DEV_LOGIN_ENABLED=1 — вхід в обхід Mix увімкнено. Не для сервера.")
    # Агрегати активності оновлюються фоново: рахувати теплокарту з мільйонів
    # рядків журналу при кожному відкритті сторінки не вийде.
    agg = asyncio.create_task(activity.background_loop())
    # Мʼяка квота і обслуговування партицій журналу.
    qm = asyncio.create_task(quota.background_loop())
    # Показники процесу: рахуються наперед з тієї ж причини, що й агрегати —
    # for_student() читає весь журнал студента разом із текстами запитів.
    ind = asyncio.create_task(indicators.background_loop())
    yield
    agg.cancel()
    qm.cancel()
    ind.cancel()
    await close_pools()


app = FastAPI(title="Wireward", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def _no_referrer(request: Request, call_next):
    """Поки OIDC_PKCE вимкнено (Mix, ADR-012), code у query /auth/callback —
    єдине, що стоїть між чужою сесією і нашою. Referer з ним не має піти
    нікуди, включно з нашими ж /static/*."""
    resp = await call_next(request)
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


# ----------------------------------------------------------------- допоміжне
async def current_student(request: Request) -> dict | None:
    sid = read_session(request).get("student_id")
    return await students.get_by_id(UUID(sid)) if sid else None


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("csrf", read_session(request).get("csrf", ""))
    ctx.setdefault("dev_login", settings.dev_login)
    ctx.setdefault("build_commit", settings.build_commit)
    ctx.setdefault("build_date", settings.build_date)
    locale = i18n.locale_of(request)
    # Встановлюється на спільному Environment безпосередньо перед рендером:
    # безпечно, бо між цим викликом і фактичним рендером немає await —
    # інший запит не встигне підмінити переклад під ногами.
    templates.env.install_gettext_translations(i18n.translations(locale), newstyle=True)
    ctx.setdefault("lang", locale)
    params = dict(request.query_params)
    ctx.setdefault("lang_urls", {
        l: f"{request.url.path}?{urlencode({**params, 'lang': l})}" for l in i18n.LOCALES})
    resp = templates.TemplateResponse(request=request, name=name, context=ctx,
                                      status_code=status_code)
    if request.query_params.get("lang") in i18n.LOCALES:
        resp.set_cookie(i18n.COOKIE_NAME, locale, max_age=180 * 86400, samesite="lax")
    return resp


# ---------------------------------------------------------- сторінки помилок
# Дефолтні відповіді FastAPI/Starlette для 4хх/5хх — голий JSON чи текст без
# стилю кабінету. Три обробники нижче покривають усе: конкретні HTTPException
# (404/403/429/...), помилки валідації форм (422 → показуємо як 400, деталі
# студентові не потрібні) і будь-який необроблений виняток (500 — трасування
# лишається лише в логах, студент бачить загальну фразу).
# Пари (сирий, неперекладений заголовок; такий самий текст) потрібні окремо
# від перекладених рядків нижче: exc.detail на явних raise завжди українською
# (пишеться в коді до того, як застосунок дізнається мову запиту), тож
# звіряти його з перекладеним заголовком сенсу нема.
_ERROR_COPY_RAW = {
    403: "Доступ заборонено",
    404: "Сторінку не знайдено",
    429: "Забагато спроб",
}


def _translated_copy(_, status_code: int) -> tuple[str, str]:
    """Літеральні `_("...")` на кожну гілку, а не змінна title/message: без
    буквального рядка `pybabel extract` цей текст не покладе в каталог
    перекладу (див. коментар вище про пастку з макросами — тут та сама
    причина, чому не можна "узагальнювати" рядок через змінну)."""
    if status_code == 403:
        return _("Доступ заборонено"), _("У вас немає прав для цієї дії чи сторінки.")
    if status_code == 404:
        return _("Сторінку не знайдено"), _("Такої адреси немає, або вона застаріла.")
    if status_code == 429:
        return _("Забагато спроб"), _("Зачекайте трохи і спробуйте ще раз.")
    return _("Помилка"), _("Щось пішло не так.")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    _ = i18n.translator(request)
    if exc.status_code == 400 and exc.detail == "CSRF":
        # "CSRF" — внутрішня позначка для розробників, а не текст для
        # студента. Найчастіша причина цієї гілки — сесія в фоновій вкладці
        # пережила MAX_AGE (security.py): це нормальний стан, не збій.
        title = _("Сесія застаріла")
        detail = _("Оновіть сторінку і спробуйте ще раз.")
    else:
        title, message = _translated_copy(_, exc.status_code)
        # exc.detail — стандартна англійська фраза Starlette ("Not Found"
        # тощо), коли її не задали явно: така не годиться студенту замість
        # перекладеного тексту вище. Показуємо лише те, що дійсно поставив
        # код застосунку.
        stock = http.client.responses.get(exc.status_code)
        raw_title = _ERROR_COPY_RAW.get(exc.status_code)
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail not in (stock, raw_title) else None
        detail = detail or message
    resp = render(request, "http_error.html", status_code=exc.status_code,
                 error_title=title, error_message=detail)
    if exc.headers:
        resp.headers.update(exc.headers)
    return resp


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    _ = i18n.translator(request)
    return render(request, "http_error.html", status_code=400,
                 error_title=_("Некоректний запит"),
                 error_message=_("Перевірте введені дані та спробуйте ще раз."))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    log.exception("необроблений виняток на %s %s", request.method, request.url.path)
    _ = i18n.translator(request)
    return render(request, "http_error.html", status_code=500,
                 error_title=_("Внутрішня помилка"),
                 error_message=_("Ми вже знаємо про проблему. Спробуйте, будь ласка, пізніше."))


# ----------------------------------------------------------------- службове
@app.get("/healthz", include_in_schema=False)
async def healthz():
    await fetchone("SELECT 1")
    return {"status": "ok"}


# ----------------------------------------------------------------- вхід
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    student = await current_student(request)
    if not student:
        return render(request, "landing.html")
    return RedirectResponse(_next_step(student), status_code=303)


def _next_step(student: dict) -> str:
    """Куди вести студента. Порядок питань не випадковий.

    Спершу технічна згода — без неї не працює курс. Потім, окремим екраном,
    питання про дослідження: злиті в одну кнопку, вони перетворили б відмову
    від участі на відмову від дисципліни, і згода перестала б бути вільною.
    """
    if student["consented_at"] is None:
        return "/consent"
    if student["research_consent"] == "pending":
        return "/research"
    return "/cabinet"


def _is_prefetch(request: Request) -> bool:
    """Спекулятивний перехід (розширення, Chrome prefetch) не має починати
    вхід: /auth/login затирає сесію беззастережно, тож для вже залогіненого
    студента такий перехід тихо його розлогінить, а для двох одночасних
    вкладок — один заїзд ламає інший (див. `pending` нижче)."""
    h = request.headers
    return (h.get("sec-purpose", "").startswith("prefetch")
            or h.get("purpose", "").lower() == "prefetch"
            or h.get("x-moz", "").lower() == "prefetch")


@app.get("/auth/login")
async def auth_login(request: Request):
    session = read_session(request)
    if session.get("student_id"):
        return RedirectResponse("/", status_code=303)
    if _is_prefetch(request):
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    verifier, challenge = oidc.new_pkce()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    url = await oidc.authorization_url(state, nonce, challenge)
    resp = RedirectResponse(url, status_code=303)
    # no-store: заборонити кешувати відповідь із Set-Cookie. Сам no-store не
    # зупиняє повторний ЗАЇЗД (браузер/розширення можуть виконати /auth/login
    # ще раз, а не відтворити кешовану відповідь) — від цього боронить pending
    # нижче: останні спроби входу зберігаються паралельно, а не одна одну
    # затирають.
    resp.headers["Cache-Control"] = "no-store"
    pending = (session.get("pending") or [])[-2:]
    pending.append({"state": state, "nonce": nonce, "pkce": verifier})
    write_session(resp, {"pending": pending, "csrf": secrets.token_urlsafe(24)})
    return resp


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    _ = i18n.translator(request)
    if error:
        return render(request, "error.html",
                      message=_("LMS повернув помилку: %(error)s") % {"error": error})

    session = read_session(request)
    match = next((p for p in session.get("pending") or []
                  if state and consteq(p["state"], state)), None)
    if match is None:
        return render(request, "error.html",
                      message=_("Параметр state не збігається. Почніть вхід заново."))
    if not code:
        return render(request, "error.html",
                      message=_("LMS не повернув код авторизації."))

    tokens = await oidc.exchange_code(code, match.get("pkce", ""),
                                      optional_secret("oidc_client_secret"))
    # Підпис id_token не перевіряємо і ключа Mix не тримаємо — ADR-012.
    # Особу беремо з /oauth/userinfo, довіра до TLS-каналу token endpoint.
    claims = await oidc.userinfo(tokens["access_token"])
    return await _finish_login(oidc.normalize_identity(claims), actor="student")


@app.get("/auth/dev", response_class=HTMLResponse)
async def dev_login_form(request: Request):
    _require_dev_login()
    return render(request, "dev_login.html")


@app.post("/auth/dev")
async def dev_login(sub: str = Form(...), email: str = Form(""), name: str = Form("")):
    """Вхід в обхід Mix — лише поки LMS недоступна.

    Кожен такий вхід пишеться в db_events, а кабінет показує банер: журнал, у
    якому не видно, що особа не підтверджена LMS, гірший за відсутність журналу.
    """
    _require_dev_login()
    identity = {"oidc_sub": sub, "email": email or None,
                "display_name": name or None, "lms_roles": []}
    return await _finish_login(identity, actor="dev-login")


def _require_dev_login() -> None:
    if not settings.dev_login:
        raise HTTPException(status_code=404)


async def _finish_login(identity: dict, actor: str):
    # Стеля на повторний вхід (П0.8). Особу засвідчує Mix, підбирати тут
    # нічого — обмежується ціна: кожен вхід тягне ensure_database.
    wait = ratelimit.login.hit(identity["oidc_sub"])
    if wait:
        raise HTTPException(status_code=429,
                            detail=f"Забагато входів поспіль. Спробуйте за {int(wait) + 1} с.")

    try:
        student = await students.upsert_from_claims(identity)
    except UniqueViolation:
        # Пошта вже привʼязана до ІНШОГО oidc_sub — obs.students має унікальний
        # індекс на lower(email) (постійна умова, не наша забаганка). Найчастіша
        # причина: студент раніше заходив під іншим кабінетом LMS з тією ж
        # поштою. Це очікуваний конфлікт даних, а не збій застосунку — 409, не 500.
        raise HTTPException(
            status_code=409,
            detail="З цією поштою вже зареєстровано інший обліковий запис. "
                   "Увійдіть під ним або зверніться до викладача.")
    sid = student["id"]

    await provisioning.ensure_database(sid, actor=actor)
    if actor == "dev-login":
        await provisioning.log_event(sid, "dev_login", actor,
                                     note="вхід в обхід Mix (DEV_LOGIN_ENABLED)")

    resp = RedirectResponse(_next_step(student), status_code=303)
    # Ротація сесії при вході: попередній ідентифікатор не переноситься.
    write_session(resp, {"student_id": str(sid), "csrf": secrets.token_urlsafe(24)})
    return resp


@app.post("/auth/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    if not csrf_ok(read_session(request), csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    resp = RedirectResponse("/", status_code=303)
    clear_session(resp)
    return resp


# ----------------------------------------------------------------- згода
@app.get("/consent", response_class=HTMLResponse)
async def consent_form(request: Request):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if student["consented_at"]:
        return RedirectResponse("/cabinet", status_code=303)
    return render(request, "consent.html", student=student)


@app.post("/consent")
async def consent_submit(request: Request, csrf_token: str = Form("")):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if not csrf_ok(read_session(request), csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    await students.give_consent(student["id"])
    await provisioning.log_event(student["id"], "consent_given", "student")
    return RedirectResponse("/research", status_code=303)


# ------------------------------------------------- згода на дослідження (П0.2)
# Окремо від попередньої, і це головне в цьому екрані. `consented_at` — підстава
# ВЕСТИ журнал: без неї курс не працює. `research_consent` — підстава включити
# студента у ВИБІРКУ статті: без неї курс працює точно так само.
#
# Що відмова нічого не змінює — не декларація, а властивість коду: поле не
# читає ні провізіонінг, ні проксі, ні жоден екран з оцінками. Єдиний його
# споживач — вʼю obs.research_cohort.
@app.get("/research", response_class=HTMLResponse)
async def research_consent_form(request: Request, error: str = ""):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if student["consented_at"] is None:
        return RedirectResponse("/consent", status_code=303)
    return render(request, "research.html", student=student, error=error)


@app.post("/research")
async def research_consent_submit(request: Request, csrf_token: str = Form(""),
                                  decision: str = Form("")):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if not csrf_ok(read_session(request), csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    if decision not in ("given", "declined"):
        # Порожнє/невідоме значення — назад на форму з поясненням, а не сирий
        # 422: pending — легальний стан (ADR каже саме так), не відмова.
        return RedirectResponse(
            f"/research?error={quote('оберіть один із варіантів')}", status_code=303)
    await students.set_research_consent(student["id"], decision)
    # Пишуться обидві події: відмова — теж рішення, і слід від неї потрібен
    # рівно так само. Мовчання в журналі не має виглядати як «не питали».
    await provisioning.log_event(student["id"], f"research_consent_{decision}", "student")
    return RedirectResponse("/cabinet", status_code=303)


# ------------------------------------------------------------ анкети (П2.6)
# Дві окремі: SUS — про засіб, рефлексія — про роботу. Заповнення не залежить
# від дослідницької згоди: відмова від участі не має заодно забирати право
# сказати, що засіб незручний. У вибірку статті потім піде лише когорта.
@app.get("/survey", response_class=HTMLResponse)
async def survey_form(request: Request, saved: int = 0):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    return render(request, "survey.html", student=student, saved=saved,
                  sus_items=surveys.SUS_ITEMS, sus_scale=surveys.SUS_SCALE,
                  reflection_items=surveys.REFLECTION_ITEMS,
                  mine=await surveys.mine(student["id"]))


@app.post("/survey/{kind}")
async def survey_submit(request: Request, kind: str, csrf_token: str = Form("")):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if not csrf_ok(read_session(request), csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    form = await request.form()
    if kind == "sus":
        answers = {f"q{i}": int(form.get(f"q{i}", 0)) for i in range(1, 11)}
    elif kind == "reflection":
        answers = {k: (form.get(k) or "").strip() for k in surveys.REFLECTION_ITEMS}
        if not any(answers.values()):
            return RedirectResponse("/survey", status_code=303)
    else:
        raise HTTPException(status_code=404)
    try:
        await surveys.save(student["id"], kind, answers)
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail="відповіді неповні")
    return RedirectResponse("/survey?saved=1", status_code=303)


# ----------------------------------------------------------------- довідка
# Інструкція студентові живе сторінкою в самому кабінеті, а не файлом у
# розсилці: роздатковий PDF губиться до другого заняття, а посилання на нього
# застаріває мовчки. Тут же поруч і рядок підключення, і поточний стан квоти.
@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    return render(request, "help.html", student=student)


# ----------------------------------------------------------------- кабінет
@app.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, token: str | None = None, limited: int = 0):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if student["consented_at"] is None:
        return RedirectResponse("/consent", status_code=303)

    # Перший вихід у кабінет: пароля ще не існує, і змушувати натискати
    # кнопку заради того, що знадобиться одразу ж (З25), — зайвий крок.
    # Видаємо один раз і переходимо на той самий URL з ?token=, щоб показ
    # ішов тим самим шляхом fresh_token, що й після ротації, і повторне
    # відкриття /cabinet (без token) більше не перевидавало пароль.
    if student["db_token_set_at"] is None and token is None:
        issued = new_db_token()
        await students.set_token(student["id"], hash_token(issued), settings.token_ttl_days)
        await provisioning.log_event(student["id"], "credential_issued", "student")
        return RedirectResponse(f"/cabinet?token={issued}", status_code=303)

    slot = None
    if student["slot"] is not None:
        # Розмір беремо з кешу, який оновлює фоновий job: рахувати
        # pg_tablespace_size на кожне відкриття кабінету зайве.
        slot = await fetchone(
            "SELECT tablespace_name, quota_bytes, coalesce(used_bytes, 0) "
            "FROM obs.ts_slots WHERE slot = %s", (student["slot"],))
    used = slot[2] if slot else 0
    quota_bytes = slot[1] if slot else 0

    events = await fetchall(
        "SELECT event, actor, at FROM obs.db_events WHERE student_id = %s "
        "ORDER BY at DESC LIMIT 10", (student["id"],))

    # Дзеркало (П2.5): студент бачить рівно ті самі числа, що й викладач.
    # Це не бонус, а умова чесності: журнал, у якому про людину рахують те,
    # чого їй не показують, — це не прозорість, а односторонній нагляд. Тими
    # самими даними студент може й скористатися: на захисті видно, що він
    # дійшов сам.
    ind = await indicators.cached(str(student["id"]), 0)

    # «Де я зараз» — те саме, що бачить викладач на картці. Студентові воно
    # корисніше, ніж покриття всіх лаб таблицею: показує, що лишилося не
    # відтворено саме в тій роботі, до якої він повертався востаннє.
    here = labs.where_now(await labs.for_student(str(student["id"])))

    return render(request, "cabinet.html",
                  student=student,
                  db_host=settings.public_host,
                  port_primary=settings.port_primary,
                  port_fallback=settings.port_fallback,
                  used=used, quota=quota_bytes,
                  pct=min(100, round(used * 100 / quota_bytes)) if quota_bytes else 0,
                  events=events,
                  fresh_token=token,
                  limited=limited,
                  ind=ind, here=here)


@app.post("/cabinet/token")
async def rotate_token(request: Request, csrf_token: str = Form("")):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if not csrf_ok(read_session(request), csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    # Токен без згоди не видаємо. Перевірка стоїть саме тут, а не лише на
    # /cabinet: CSRF-токен видно й на сторінці згоди, тож редіректу з кабінету
    # замало — POST сюди проходив в обхід, і токен видавався. Перевірено.
    if student["consented_at"] is None:
        return RedirectResponse("/consent", status_code=303)
    # Обмеження частоти (П0.8): кожна ротація коштує argon2 і, головне, ламає
    # вже налаштоване підключення студента в DBeaver. Відмова видима — мовчазна
    # тут гірша за відсутність ліміту.
    wait = ratelimit.token_rotation.hit(str(student["id"]))
    if wait:
        if ratelimit.token_rotation.report_once(str(student["id"])):
            await provisioning.log_event(student["id"], "rate_limited", "student",
                                         where="token_rotate")
        return RedirectResponse(f"/cabinet?limited={int(wait) + 1}", status_code=303)
    token = new_db_token()
    await students.set_token(student["id"], hash_token(token), settings.token_ttl_days)
    await provisioning.log_event(student["id"], "credential_rotated", "student")
    # Токен показується рівно один раз: у базі лишається тільки argon2id-хеш.
    return RedirectResponse(f"/cabinet?token={token}", status_code=303)


@app.post("/cabinet/recreate")
async def recreate_db(request: Request, csrf_token: str = Form("")):
    student = await current_student(request)
    if not student:
        return RedirectResponse("/", status_code=303)
    if not csrf_ok(read_session(request), csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    await provisioning.drop_database(student["id"])
    await provisioning.log_event(student["id"], "dropped", "student")
    await provisioning.ensure_database(student["id"], actor="student",
                                      trigger="recreate_button")
    return RedirectResponse("/cabinet", status_code=303)


# ----------------------------------------------------------------- адмінка
# Чотири екрани живуть в admin.py. render і current_student передаємо явно,
# щоб не заводити імпорт по колу.
admin.register(app, render, current_student)
