# Despliegue desde GitHub Actions + Supabase + Streamlit Cloud (sin VPS, sin Telegram)

Procedimiento exacto, en orden. Cada paso se puede repetir sin romper nada (todo es idempotente).

## 0. Secretos del repositorio (GitHub → Settings → Secrets and variables → Actions)

| Secreto | Obligatorio | Para qué | Cómo se genera |
|---|---|---|---|
| `MOTOTRADE_DATABASE_URL` | **Sí** | Conexión a Supabase | Supabase → *Connect* → **Session pooler** → copiar la URI y reemplazar el prefijo `postgresql://` por `postgresql+psycopg://`. Formato: `postgresql+psycopg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`. **No** usar la conexión directa (`db.<ref>.supabase.co`): es IPv6 y falla desde GitHub/Streamlit. |
| `MOTOTRADE_SECRET_KEY` | **Sí** | Firma de las sesiones del panel | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `MOTOTRADE_ADMIN_PASSWORD` | Sí para crear el admin | Contraseña inicial del usuario `admin` (≥ 8 caracteres) | La elegís vos; nunca se imprime ni se registra |
| `MOTOTRADE_ADMIN_USERNAME` | No (default `admin`) | Nombre del admin inicial | — |
| `MOTOTRADE_SUPABASE_URL` | Recomendado | Fotos en Supabase Storage | `https://<ref>.supabase.co` |
| `MOTOTRADE_SUPABASE_SERVICE_KEY` | Recomendado | Fotos en Supabase Storage (bucket privado `fotos`) | Supabase → Project Settings → API → **service_role** |
| `MOTOTRADE_SUPABASE_BUCKET` | No (default `fotos`) | Nombre del bucket | — |
| `MOTOTRADE_API_KEY` | **No** | Solo para usar la API HTTP con `X-API-Key` desde scripts externos. El panel y el worker no la usan. | Si algún día la necesitás: `python -c "import secrets;print(secrets.token_urlsafe(32))"` (mínimo 16 caracteres; vacía = API por clave deshabilitada) |
| `MOTOTRADE_TELEGRAM_BOT_TOKEN` / `MOTOTRADE_TELEGRAM_CHAT_ID` | **No** | Copia de las alertas por Telegram | Dejar sin definir. El centro de notificaciones de la app es el canal principal. |
| `MOTOTRADE_ANTHROPIC_API_KEY` / `MOTOTRADE_LLM_PROVIDER` / `MOTOTRADE_LLM_MODEL` | No | IA real (redacción e inspección de fotos) | Sin clave, proveedor `fake` (plantillas). Si la cargás: `MOTOTRADE_LLM_PROVIDER=anthropic`, modelo sugerido `claude-haiku-5-5`. |

Los secretos nunca aparecen en los logs: GitHub los enmascara y el CLI imprime la URL de la base sin credenciales.

## 1. Crear las tablas en Supabase (workflow manual `setup-database`)

GitHub → **Actions** → **setup-database** → **Run workflow** → dejar `create_admin = true` e `initial_capital = true` → Run.

Qué hace, en orden: verifica que existan los secretos obligatorios (sin mostrarlos) → `alembic upgrade head` (crea las
34 tablas y registra la versión del esquema) → crea el usuario admin **solo si no hay ningún usuario** → registra el
capital inicial **solo si el libro de caja está vacío** → imprime `mototrade check` (estado operativo, sin secretos).

Verificación: el run termina en verde y el último paso muestra `Migraciones: aplicada=… · última=…` iguales y
`Esquema al día`. En Supabase → Table Editor aparecen las tablas (`users`, `listings`, `deals`, `notifications`…).

Si falla: el mensaje `::error::Falta el secreto …` indica cuál cargar. Un error de conexión suele ser usar la URI
directa en vez del pooler, o la contraseña con caracteres especiales sin codificar (`@` → `%40`, `#` → `%23`).

## 2. Publicar el panel en Streamlit Community Cloud

1. https://share.streamlit.io → **New app** → repo `federicogabrielmartinez92-create/mototrade-ai`, rama `main`,
   archivo `ui/app.py`.
2. **Advanced settings → Secrets**: pegar el contenido de `.streamlit/secrets.toml.example` con tus valores
   (`MOTOTRADE_DATABASE_URL` = el mismo pooler, `MOTOTRADE_SECRET_KEY` = la misma clave que en GitHub,
   `MOTOTRADE_ADMIN_PASSWORD`, `MOTOTRADE_SUPABASE_URL`, `MOTOTRADE_SUPABASE_SERVICE_KEY`). Lo demás puede quedar vacío.
3. Deploy. Visibilidad **privada**; agregá tu email como viewer.
4. Entrá con `admin` y la contraseña de `MOTOTRADE_ADMIN_PASSWORD`. Si el paso 1 no creó el admin, el propio
   panel lo crea en el primer arranque a partir del secreto (solo si no existe ningún usuario) y te lo avisa.

Desde el panel: cambiar la contraseña del admin creando otro usuario admin en *Configuración de agentes → Usuarios*
y, si querés, borrar el secreto `MOTOTRADE_ADMIN_PASSWORD` de GitHub y Streamlit (ya no se necesita).

## 3. Activar el worker programado (`worker`)

Corre solo cada 30 minutos (cron). Para la primera corrida: Actions → **worker** → Run workflow. No migra: si
el esquema está desactualizado se detiene con `FALTAN MIGRACIONES` y te manda al paso 1. Cada corrida:
analiza anuncios nuevos, crea oportunidades, revisa precios, deja las alertas en **🔔 Notificaciones** del panel
y mantiene activo el proyecto de Supabase.

Tras cambios de esquema en el futuro: primero `setup-database` (con `create_admin=false`), después el worker sigue.

## 4. Operar sin Telegram

- Cargar anuncios: **Radar → Cargar anuncio** (marca, modelo, año, km, precio, enlace, vendedor, estado, fotos) o
  **Importar CSV**. Las fotos se suben desde el formulario o desde la pestaña *Anuncios* y en *Inspección visual*.
- Ver alertas: campana en la barra lateral y página **Notificaciones** (filtros, marcar leídas).
- Analizar sin esperar al cron: **Radar → Analizar anuncios ahora**.

## 5. Mantenimiento

- `keepalive` (semanal): latido en el repo + backup `pg_dump` como artifact (14 días). Supabase Free no tiene backups.
- Para ver el estado desde Actions sin tocar nada: `worker` → Run workflow; el paso "Esquema al día" y el `check`
  final muestran todo.

## 6. Si el panel dice que la base no está lista: diagnóstico

La pantalla de login muestra un **diagnóstico clasificado y redactado** (sin contraseñas ni URL completa):

| Categoría | Significado | Qué hacer |
|---|---|---|
| `missing_url` | A Streamlit no le llegó `MOTOTRADE_DATABASE_URL` y estaría usando un SQLite local vacío | En App settings → Secrets la clave debe ser exactamente `MOTOTRADE_DATABASE_URL = "postgresql+psycopg://…"` (con comillas, TOML). Guardar y **Reboot app**. El expander "Secretos detectados" lista qué nombres llegaron. |
| `url_invalid` | La URL no se interpreta | Prefijo `postgresql+psycopg://`, sin comillas extra ni espacios; caracteres especiales de la contraseña codificados |
| `dns` | El host no existe | Copiar el host del Session pooler desde Supabase |
| `network` | No responde | Proyecto de Supabase pausado (Resume) o puerto equivocado (usar 5432) |
| `ssl` | Fallo TLS | Agregar `?sslmode=require` |
| `tenant` | "Tenant or user not found" | El usuario del pooler es `postgres.<ref>` |
| `auth` | Contraseña incorrecta | Es la contraseña de la base (Project Settings → Database), no la del login |
| `schema_pending` | Conecta pero faltan migraciones **o es otra base** | Comparar host/usuario del diagnóstico con el paso "Diagnóstico de conexión" del workflow `worker`/`setup-database`: deben coincidir. Si coinciden, correr `setup-database`. |

La misma clasificación está disponible por CLI (`python -m mototrade.cli db-check`) y como paso en los workflows.
