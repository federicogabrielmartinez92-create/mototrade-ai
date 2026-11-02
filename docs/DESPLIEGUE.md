# Despliegue en un VPS (24×7, sin depender de tu computadora)

Requisitos: un VPS Linux (2 vCPU / 2–4 GB RAM alcanza; ej. Hetzner CX22, DigitalOcean 2 GB, Contabo), un dominio
(opcional pero recomendado para HTTPS) y Docker.

## 1. Preparar el servidor (una vez)

```bash
# como root en el VPS
apt update && apt install -y ca-certificates curl git ufw
curl -fsSL https://get.docker.com | sh
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
adduser --disabled-password mototrade && usermod -aG docker mototrade
```

## 2. Subir el proyecto

```bash
su - mototrade
git clone <tu-repo-privado> mototrade-ai && cd mototrade-ai   # o subir el ZIP y descomprimir
cp .env.example .env
nano .env   # completar: POSTGRES_PASSWORD, MOTOTRADE_SECRET_KEY, MOTOTRADE_API_KEY, MOTOTRADE_ADMIN_PASSWORD,
            # Telegram (opcional), Anthropic (opcional). MOTOTRADE_ENV=production
```

Generar claves aleatorias: `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`.

## 3. Levantar

```bash
docker compose up -d --build
docker compose ps            # db, api, worker, ui, backup deben estar "healthy"/"running"
docker compose logs -f api   # la API aplica migraciones y crea el admin al arrancar
```

El panel queda en `http://127.0.0.1:8501` y la API en `http://127.0.0.1:8000` **solo dentro del VPS**.

## 4. HTTPS con Caddy (recomendado)

```bash
apt install -y caddy
cp docker/Caddyfile.example /etc/caddy/Caddyfile && nano /etc/caddy/Caddyfile   # poner tu dominio
systemctl reload caddy
```

Apuntá los DNS `panel.tu-dominio.com` y `api.tu-dominio.com` a la IP del VPS. Caddy obtiene el certificado solo.
Extra recomendado: restringir el panel por IP o agregar autenticación básica en Caddy, y activar 2FA en el VPS (SSH por clave).

## 5. Operación

| Tarea | Comando |
|---|---|
| Ver logs | `docker compose logs -f --tail=100 worker` |
| Reiniciar tras cambiar reglas | `docker compose restart api worker ui` |
| Actualizar código | `git pull && docker compose up -d --build` |
| Backup manual | `make backup` (o `docker compose exec db pg_dump -U mototrade -Fc mototrade > backups/x.dump`) |
| Restaurar | `docker compose exec -T db pg_restore -U mototrade -d mototrade --clean < backups/x.dump` |
| Verificar auditoría | `docker compose exec api python -m mototrade.cli verify-audit` |
| Estado general | `docker compose exec api python -m mototrade.cli check` |

Los backups quedan en `./backups` (rotación 14 días). **Copialos fuera del VPS** (p. ej. `rclone` a Google Drive/S3 con un cron).

## 6. Monitoreo mínimo

- Healthchecks de Docker en `api` y `ui` (`docker compose ps` muestra el estado).
- El worker registra cada corrida en la tabla `job_runs` (visible en el Dashboard).
- Alertas por Telegram: si el bot deja de avisar, revisá `docker compose logs worker`.
- Opcional: UptimeRobot/Healthchecks.io apuntando a `https://api.tu-dominio.com/health`.

## Desarrollo local (sin Docker)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .
cp .env.example .env    # con SQLite: MOTOTRADE_DATABASE_URL=sqlite:///./mototrade.sqlite3
alembic upgrade head && python -m mototrade.cli bootstrap
streamlit run ui/app.py            # panel
uvicorn mototrade.api.app:app --reload   # API (otra terminal)
python -m mototrade.cli worker --once    # una pasada del worker
python -m pytest -q                      # tests
```
