#!/usr/bin/env bash
set -euo pipefail

wait_for_db() {
  if [[ "${MOTOTRADE_DATABASE_URL:-}" == postgresql* ]]; then
    for i in $(seq 1 30); do
      if python - <<'EOF'
import sys
from sqlalchemy import create_engine, text
from mototrade.config import get_settings
try:
    with create_engine(get_settings().database_url).connect() as c:
        c.execute(text("select 1"))
except Exception as e:
    sys.exit(1)
EOF
      then return 0; fi
      echo "Esperando a la base de datos... ($i)"; sleep 2
    done
    echo "La base de datos no respondió"; exit 1
  fi
}

case "${1:-api}" in
  api)
    wait_for_db
    alembic upgrade head
    python -m mototrade.cli bootstrap
    exec uvicorn mototrade.api.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
    ;;
  worker)
    wait_for_db
    exec python -m mototrade.cli worker
    ;;
  ui)
    wait_for_db
    exec streamlit run ui/app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true \
      --browser.gatherUsageStats false --server.enableXsrfProtection true
    ;;
  backup)
    # Dump diario de PostgreSQL a /app/backups (montar como volumen y copiar FUERA del VPS)
    while true; do
      ts=$(date +%Y%m%d_%H%M%S)
      if pg_dump "${MOTOTRADE_DATABASE_URL/postgresql+psycopg/postgresql}" -Fc -f "/app/backups/mototrade_${ts}.dump"; then
        echo "Backup OK: mototrade_${ts}.dump"
        find /app/backups -name 'mototrade_*.dump' -mtime +14 -delete
      else
        echo "Backup FALLÓ"
      fi
      sleep "${BACKUP_INTERVAL_SECONDS:-86400}"
    done
    ;;
  *)
    exec "$@"
    ;;
esac
