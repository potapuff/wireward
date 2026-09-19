FROM postgres:18-bookworm AS base

# JIT-компіляція запитів (LLVM) тут не потрібна — навчальний стенд з
# короткими лабораторними запитами, а не аналітичне навантаження, на яке
# JIT і розрахований. Тягне з собою ~170 МБ (libllvm19, libz3-4).
RUN apt-get remove -y --purge postgresql-18-jit \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# --- wal-g -------------------------------------------------------------------
# Офсайт-бекап фізичного кластера просто в S3-сумісне сховище (ADR-022):
# archive_command пише WAL, окрема ціль — базовий бекап. pgbackrest відкинуто
# як важчий: на 4 ГБ сервері важить кожен резидентний процес, а wal-g — один
# статичний бінарник, який живе рівно стільки, скільки триває виклик.
# Версія і контрольна сума прибиті: бінарник тягнеться з мережі під час
# збірки, і «останній реліз» — це змінна, якої в образі бути не повинно.
ARG WALG_VERSION=v3.0.9
ARG WALG_ASSET=wal-g-pg-22.04-amd64
ARG WALG_SHA256=4f03ee4679db7f660bfd1b2b8291ac5df247cd2ec48074b60c112f4897299f24
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL -o /tmp/wal-g.tar.gz \
      "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${WALG_ASSET}.tar.gz"; \
    echo "${WALG_SHA256}  /tmp/wal-g.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/wal-g.tar.gz -C /tmp; \
    install -m 0755 "/tmp/${WALG_ASSET}" /usr/local/bin/wal-g; \
    rm -rf /tmp/wal-g.tar.gz "/tmp/${WALG_ASSET}"; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    wal-g --version
# ca-certificates лишається навмисно: реальний S3 — це TLS, і без кореневих
# сертифікатів wal-g мовчки не з'єднається саме на сервері, а не в тесті.

# Каталог під tablespace'и студентів. Власника виставляємо тут, щоб іменований
# том успадкував права і initdb-скрипт міг створювати в ньому слоти.
RUN mkdir -p /srv/pgts && chown postgres:postgres /srv/pgts && chmod 700 /srv/pgts

# Конфіги лежать у шарі образу лише як ЗРАЗКИ; робочий каталог
# /etc/postgresql — tmpfs, і entrypoint розкладає їх туди при кожному старті.
# Так корінь контейнера можна тримати read-only: рендер pg_hba.conf при
# кожному старті (див. entrypoint.sh) інакше в нього впирається.
COPY conf/postgresql.conf /usr/local/share/observer/postgresql.conf
COPY conf/pg_hba.conf.tmpl /usr/local/share/observer/pg_hba.conf.tmpl
COPY entrypoint.sh /usr/local/bin/observer-entrypoint.sh
COPY walg/walg /usr/local/bin/walg
COPY walg/walg-archive.sh /usr/local/bin/walg-archive.sh
COPY initdb/ /docker-entrypoint-initdb.d/

RUN chown -R postgres:postgres /usr/local/share/observer \
 && chmod +x /usr/local/bin/observer-entrypoint.sh \
              /usr/local/bin/walg /usr/local/bin/walg-archive.sh

# --- сплющення шарів -----------------------------------------------------
# apt-get remove/purge вище лише додає шар з "whiteout"-мітками — байти
# postgresql-18-jit і решти base-образу фізично лишаються в нижніх шарах і
# так само тягнуться при pull (перевірено docker history: шар видалення
# важить 475 КБ, шар встановлення — 336 МБ). Єдиний спосіб реально
# позбутися їх у межах Debian — сплющити файлову систему одним COPY в
# порожній фінальний стейдж; ENTRYPOINT/CMD/ENV/EXPOSE/VOLUME з базового
# образу при цьому губляться і переносяться вручну (перевірено
# `docker inspect postgres:18-bookworm` — саме ці значення офіційний образ
# виставляє і на які спирається docker-entrypoint.sh: PATH — бінарники
# postgres, PG_MAJOR — шлях до PGDATA за замовчуванням, LANG — локаль).
FROM scratch
COPY --from=base / /
ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/18/bin" \
    LANG=en_US.utf8 \
    PG_MAJOR=18
EXPOSE 5432
VOLUME /var/lib/postgresql
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/local/bin/observer-entrypoint.sh"]
CMD ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
