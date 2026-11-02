# Arquitectura en nube gratuita (USD 0–15/mes), sin VPS

Verificado el 2 de noviembre de 2026 contra documentación oficial. Las restricciones cambian: revisar las fuentes
antes de confiar en una cifra. Docker (`docker-compose.yml`) sigue funcionando para desarrollo local y para migrar
a un VPS después sin tocar el código.

## Stack

```
 Celular (Telegram /moto + fotos) ──┐
                                    ▼
 GitHub Actions (cron cada 30 min) ── worker --once ──▶ Supabase Postgres (pooler, session mode)
      │  lee Telegram, analiza, notifica                       ▲          └── Supabase Storage (fotos JPEG ≤ 400 KB)
      └──▶ Telegram (alertas al operador)                      │
 Streamlit Community Cloud (1 app privada) ── panel ───────────┘
 Anthropic API (Haiku 5.5 por defecto) bajo demanda, con presupuesto del Centro de Control
```

| Servicio | Plan free | Límites clave | Riesgo de suspensión | Cómo lo mitigamos |
|---|---|---|---|---|
| **Streamlit Community Cloud** | Gratis | ~690 MB–2,7 GB RAM; **1 sola app privada**; sin tareas de fondo; secretos en TOML | **Duerme a las 12 h sin tráfico**; despierta con un clic de cualquier viewer | Es un panel interno: aceptar que duerma. El worker no corre ahí. |
| **Supabase Free** | Gratis | 500 MB DB, 1 GB Storage (50 MB/archivo), 5 GB egress, 2 proyectos, **sin backups** | **Pausa a los 7 días sin actividad**; restaurable 1 año | El worker consulta la base cada 30 min (actividad diaria). Backup semanal por `keepalive.yml` (artifact 14 días). Conexión por **pooler en modo session** (puerto 5432 del host `pooler.supabase.com`): la directa es IPv6 y falla desde GitHub/Streamlit. |
| **GitHub Actions** | USD 12/mes de cómputo ≈ **2.000 min Linux** en repo privado; ilimitado en público | cron mínimo 5 min; retrasos en picos; cron se desactiva a los 60 días sin actividad (documentado para repos públicos) | `worker.yml` cada 30 min (≈1.440 min/mes si la corrida dura ≤1 min); `keepalive.yml` hace un commit semanal; `workflow_dispatch` para correr a mano. |
| **Telegram Bot API** | Gratis | `getUpdates` guarda updates **24 h**; `getFile` ≤ 20 MB | Si el worker falta >24 h se pierden mensajes | Corrida cada 30 min + offset persistido en `system_state`; si hubo caída larga, reenviar `/moto`. |
| **Anthropic API** | Pago por uso | Haiku 5.5 US$0,10/0,50 por MTok; Sonnet 5.5 US$2/10 | — | Presupuesto mensual/diario y cupos de visión en el Centro de Control; caché por entrada idéntica; filtros deterministas antes de llamar. |
| **Cloudflare Workers** (opcional) | Gratis | 100k req/día, 5 cron triggers | — | Solo si hiciera falta un "despertador" extra o un webhook relay. No corre Python. |

## Qué NO va a funcionar en este stack (y qué sí)

- **No** hay worker *always-on*: la latencia de respuesta por Telegram es de hasta 30 min (5 min mínimo técnico, y
  consumiría la cuota). Para respuestas inmediatas hace falta un VPS (Docker) o el plan B.
- **No** hay webhooks (ni de Telegram ni de ningún canal) sin un servicio adicional encendido.
- **No** hay tareas de fondo dentro de Streamlit Cloud: la app duerme a las 12 h y se reinicia sin aviso.
- **No** usar la conexión directa de Supabase (IPv6) desde GitHub/Streamlit.
- **Sí** funciona todo el flujo manual asistido: cargar anuncios desde el celular, análisis automático cada 30 min,
  alertas por Telegram, aprobaciones desde el panel, inspección con IA bajo presupuesto, finanzas y auditoría.

## Costo estimado mensual

Streamlit 0 + Supabase 0 + GitHub Actions 0 (dentro de cuota) + Telegram 0 + Anthropic **US$ 0,3–7** según modelo y
volumen (≈1.000 mensajes/mes con Haiku 5.5 ≈ US$0,35; con Sonnet 5.5 ≈ US$7; 60 inspecciones visuales de 6 fotos
con Haiku ≈ US$0,10). Total realista: **US$ 0–10/mes**.

## Pasos de puesta en marcha (≈ 45 min)

1. **Supabase**: crear proyecto Free (región más cercana: `sa-east-1` São Paulo). En *Project Settings → Database*
   copiar la connection string del **Session pooler** (no la directa). Crear bucket privado `fotos` en Storage.
   Copiar la **service role key** (secreto).
2. **GitHub**: repo (privado o público). Secrets del repo: `MOTOTRADE_DATABASE_URL` (pooler), `MOTOTRADE_SECRET_KEY`,
   `MOTOTRADE_API_KEY`, `MOTOTRADE_TELEGRAM_BOT_TOKEN`, `MOTOTRADE_TELEGRAM_CHAT_ID`, `MOTOTRADE_ANTHROPIC_API_KEY`
   (opcional), `MOTOTRADE_LLM_PROVIDER` (`anthropic` o `fake`), `MOTOTRADE_SUPABASE_URL`, `MOTOTRADE_SUPABASE_SERVICE_KEY`.
   Ejecutar `worker.yml` a mano una vez (Actions → worker → Run workflow): aplica migraciones y crea el esquema.
3. **Admin inicial**: desde tu PC, con `.env` apuntando al pooler: `python -m mototrade.cli bootstrap` (crea el admin
   con `MOTOTRADE_ADMIN_PASSWORD` y el capital inicial). O agregá `MOTOTRADE_ADMIN_PASSWORD` a los secrets de
   Streamlit y entrá: el panel lo crea si no hay usuarios.
4. **Streamlit Community Cloud**: New app → repo → `ui/app.py`. En *Advanced settings → Secrets* pegar el contenido
   de `.streamlit/secrets.toml.example` con valores reales. Visibilidad: privada; agregar tu email como viewer.
5. **Telegram**: crear el bot con `@BotFather`, escribirle `/start`, obtener tu `chat_id` (ver `OPERACION.md`).
   Probar: `/moto Honda | Wave 110 | 2022 | 15000 | 2300000 | Rosario` con una foto adjunta; en ≤30 min aparece en el
   Radar y recibís la confirmación.
6. **Centro de Control**: fijar capital total, reserva, máximo de motos, presupuesto de IA.

## Plan B si hace falta "siempre encendido"

**Oracle Cloud Always Free** (VM ARM A1, hoy 2 OCPU / 12 GB; reclamo por ocio si CPU/red/memoria < 20 % durante 7
días): instalar Docker y usar `docker-compose.yml` tal cual, con el worker cada 5 min. Render/Fly/Railway/Koyeb/
PythonAnywhere ya no ofrecen un worker gratuito utilizable (ver fuentes).

## Fuentes

- Streamlit: https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app ·
  https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app ·
  https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management
- Supabase: https://supabase.com/pricing · https://supabase.com/docs/guides/platform/free-project-pausing ·
  https://supabase.com/docs/guides/database/connecting/connecting-to-postgres ·
  https://supabase.com/docs/guides/platform/ipv4-address · https://supabase.com/docs/guides/storage/uploads/file-limits ·
  https://supabase.com/docs/reference/api/v1-restore-a-project
- GitHub Actions: https://docs.github.com/en/billing/concepts/product-billing/github-actions ·
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows ·
  https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows ·
  https://docs.github.com/en/actions/reference/limits
- Telegram: https://core.telegram.org/bots/api · https://core.telegram.org/bots/faq
- Cloudflare Workers: https://developers.cloudflare.com/workers/platform/limits/
- Anthropic precios: https://platform.claude.com/docs/en/about-claude/pricing
- Alternativas: https://render.com/docs/free · https://docs.fly.io/about/discontinued-plans ·
  https://docs.railway.com/pricing/plans · https://www.koyeb.com/docs/faqs/pricing ·
  https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm · https://www.pythonanywhere.com/pricing/
