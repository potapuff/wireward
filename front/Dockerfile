FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Версія для cache-busting статики й footer (З16). ARG/ENV — після
# pip install: інакше кожен коміт міняв би значення й змивав кеш шару
# залежностей при кожній збірці. Значення обчислюються зовні (git
# недоступний усередині образу) і прокидаються в env через ARG.
ARG BUILD_COMMIT=dev
ARG BUILD_DATE=
ENV BUILD_COMMIT=${BUILD_COMMIT} BUILD_DATE=${BUILD_DATE}

COPY app/ ./app/

# Непривілейований користувач: контейнер дивиться назовні, root тут ні до чого.
RUN useradd --system --uid 10001 --no-create-home observer
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
