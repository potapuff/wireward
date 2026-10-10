#!/usr/bin/env bash
# Складає образи Wireward в один архів для перенесення на сервер без збірки
# там: сервер x86, ця машина теж x86 — édition однакова, тож `docker save` +
# `docker load` замінюють реєстр.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-dist}"
mkdir -p "$OUT_DIR"

echo "== перевірка / генерація секретів =="
./scripts/make-secrets.sh

IMAGES=(observer-postgres observer-front observer-pgproxy caddy:2-alpine)
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$OUT_DIR/wireward-images-$STAMP.tar"

echo "== збірка образів (docker compose build) =="
# Ті самі BUILD_COMMIT/BUILD_DATE, що в `make up` (З16) — інакше образ, що
# їде на сервер, назавжди показує «версія dev», а cache-busting не працює.
export BUILD_COMMIT="$(git rev-parse --short HEAD)$(git diff --quiet || echo -dirty)"
export BUILD_DATE="$(git show -s --format=%cI HEAD)"
docker compose build

echo "== caddy:2-alpine (pull, якщо ще немає) =="
docker pull caddy:2-alpine

for img in "${IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "образу $img немає локально — збірка не завершилась?" >&2
    exit 1
  fi
done

echo "== docker save -> $ARCHIVE =="
docker save -o "$ARCHIVE" "${IMAGES[@]}"

echo "== стиснення =="
gzip -f "$ARCHIVE"
ARCHIVE="$ARCHIVE.gz"

sha256sum "$ARCHIVE" | tee "$ARCHIVE.sha256"

cat <<EOF

Готово: $ARCHIVE

На сервері:
  gunzip -k "$(basename "$ARCHIVE")"
  docker load -i wireward-images-$STAMP.tar
  docker compose --profile server up -d   # БЕЗ --build

Звірити цілісність до розпакування:
  sha256sum -c $(basename "$ARCHIVE").sha256
EOF
