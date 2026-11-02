# Informe de entrega — Incremento 1 (2 de noviembre de 2026)

## 1. Auditoría del starter (`MotoTrade_AI_Starter.zip`)

**Contenido**: 8 archivos, 369 líneas. `core.py` (fórmula de compra máxima y política de ofertas), `db.py` (SQLite, 7
tablas), `app.py` (Streamlit monolítico de 7 pestañas con SQL embebido), 3 tests, README y ROADMAP.

**Verificación ejecutada**: `pytest` → 3/3 pasan. Streamlit headless (`AppTest`) → arranca sin excepciones, 7 pestañas,
el formulario de anuncios persiste en SQLite con fila de auditoría. Advertencia de deprecación (`use_container_width`).

**Qué se reutilizó**: la filosofía (políticas deterministas, aprobación humana, auditoría), la fórmula de compra
máxima como punto de partida (ahora generalizada con búsqueda binaria), las entidades del esquema (anuncios,
inventario, gastos, conversaciones, mensajes, reparaciones, auditoría) y los tres casos de test (reescritos y ampliados).

**Qué se reemplazó y por qué**: SQLite sin migraciones → PostgreSQL + SQLAlchemy + Alembic; SQL dentro de la interfaz →
capa de servicios por módulo; tasación ingresada a mano → motor con comparables y confianza; sin autenticación, sin
API, sin worker, sin kill switch, sin idempotencia → todo agregado; auditoría simple → cadena de hashes; estados en
dos idiomas → enums únicos.

## 2. Qué se desarrolló

- **Arquitectura** definitiva con los 12 módulos sobre una única base (31 tablas), documentada en `ARQUITECTURA.md`.
- **Reglas de negocio** versionadas (`business_rules.yaml`, validadas, inmutables, con hash registrado en cada tasación,
  decisión y aprobación). Incluyen transferencia DNRPA/sellos e impuestos (RI, IVA s/margen, IIBB) parametrizados.
- **Núcleo**: auditoría tamper-evident, cola de aprobaciones con roles, kill switch, acciones salientes idempotentes con
  reintentos, protección contra instrucciones maliciosas, autenticación Argon2id + tokens + roles.
- **Módulos** (ver tabla del README): radar, tasador, inspector, repuestos, documental, negociador, visitas,
  publicaciones, CRM, finanzas, precios dinámicos, permutas, analítica.
- **Integraciones**: Telegram (real), LLM (fake + Anthropic), adaptador Mercado Libre (parser real, cliente pendiente).
- **Worker** autónomo (5 jobs idempotentes, registro de corridas), **API** FastAPI (23 endpoints), **panel** Streamlit
  (14 secciones, login, roles, kill switch, bandeja de negociación), **CLI** de operación.
- **Infra**: Dockerfile, docker-compose (db, api, worker, ui, backup), Caddy de ejemplo, Makefile, GitHub Actions.

## 3. Pruebas ejecutadas y resultados

| Suite | Tests | SQLite | PostgreSQL 16 |
|---|---|---|---|
| `test_core.py` (dinero, auditoría, kill switch, aprobaciones, injection, auth) | 14 | ✅ | ✅ |
| `test_valuation.py` (estimación, confianza, matemática, compra máxima garantiza ROI) | 11 | ✅ | ✅ |
| `test_radar_finance.py` (CSV, dedup, precios, ML parser, scoring, compra/venta/P&L) | 9 | ✅ | ✅ |
| `test_negotiation.py` (estrategias, límites, kill switch, takeover, sospechosos, tope diario, vendedor, LLM mentiroso) | 17 | ✅ | ✅ |
| `test_modules.py` (precios, permutas, inspección, repuestos, documentos, publicaciones, CRM, visitas, analítica) | 11 | ✅ | ✅ |
| `test_worker_integrations.py` (Telegram con transporte simulado, outbound, worker) | 7 | ✅ | ✅ |
| `test_api.py` (auth, roles, ciclo completo por HTTP, CSV) | 4 | ✅ | ✅ |
| `test_ui.py` (login, 14 vistas, aprobación, kill switch, escaneo) | 6 | ✅ | ✅ |
| **Total** | **79** | **79/79** | **79/79** |

Cobertura del paquete `mototrade`: **90 %**. Además: migración Alembic aplicada en PostgreSQL y SQLite (autogenerate
posterior vacío = modelos sincronizados); `docker compose config` válido; recorrido real del CLI contra PostgreSQL
(migrar → bootstrap → check → importar 3 anuncios → worker: 2 oportunidades, 2 notificaciones → auditoría íntegra);
API (uvicorn) y panel (streamlit) levantados como procesos y respondiendo.

## 4. Funcionalidades simuladas o manuales (no las presentamos como automáticas)

| Función | Estado real | Motivo |
|---|---|---|
| Búsqueda automática de anuncios (ML, Marketplace, RosarioGarage) | **No existe**; carga manual/CSV | ML: búsqueda de terceros cerrada (403); Meta: sin API y scraping prohibido; RosarioGarage: sin API, captcha. No se hace scraping |
| Envío de mensajes a vendedores/compradores | **Manual** (vos copiás el texto aprobado) | WhatsApp Business exige opt-in; ML no permite contactar vendedores ajenos; Marketplace sin API |
| Inspector visual | Simulado sin API key; real con API key | Requiere `MOTOTRADE_ANTHROPIC_API_KEY` y fotos cargadas |
| Redacción del negociador | Plantillas sin API key; LLM con API key | Idem |
| Publicación de avisos | Manual con texto aprobado | ML requiere paquete contratado por ejecutivo; otros sin API |
| Verificación de dominio/prendas | Registro de evidencia; no oficial | No existe API pública de DNRPA |
| Búsqueda de repuestos | Carga manual de presupuestos | Sin API de proveedores |
| Notificación Telegram | Real con token; "console" sin token | Configuración de 5 minutos |
| Docker en producción | Validado sintácticamente; no se pudo construir la imagen en este entorno | Se prueba en tu VPS siguiendo `DESPLIEGUE.md` |

## 5. Errores, limitaciones y riesgos

- **Riesgo de calibración**: `listing_to_sale_discount` (6 %), `quick_sale_discount` (8 %) y las tasas impositivas son
  supuestos; hasta tener ventas reales la confianza del tasador se queda en media/baja. Cargar comparables `sold`.
- **Modelo impositivo simplificado**: IVA sobre margen aproxima el régimen de bienes usados; IIBB con base diferencial que
  el Código Fiscal reserva a "concesionarias". **Confirmar con el contador** antes de operar.
- **Costos de transferencia**: el mínimo en MRPA cambia cada cuatrimestre; hay que actualizar `transfer_registral_min`.
- **Estimación de mercado sin ajuste por km/versión**: solo marca/modelo/año ±1.
- **Entorno de prueba**: no se pudo verificar Telegram real ni Anthropic real (sin credenciales ni salida a esos hosts); el
  código está probado con transportes simulados y el cliente oficial, pero la primera llamada real debe hacerse en tu VPS.
- **Streamlit** no es ideal para móvil; se agregó CSS responsivo, pero las tablas anchas requieren scroll.
- **Rate limits de Telegram** (1 msg/s por chat) no están modelados: con decenas de alertas simultáneas podrían fallar y
  reintentarse (hasta 5 intentos).
- Encontrados y corregidos durante el desarrollo: JSON no serializaba `Decimal`; relación `conv.messages` cacheada;
  `get_engine()` creaba dos motores con SQLite en memoria; expresiones condicionales de una línea rompían el "magic" de Streamlit.

## 6. Acciones que requieren tu intervención (explicadas simple)

1. **Crear el bot de Telegram** (5 min, `OPERACION.md`) y pegar token + chat_id en `.env`.
2. **Conseguir una API key de Anthropic** si querés redacción natural e inspección de fotos (opcional).
3. **Revisar `config/business_rules.yaml` con tu contador**: capital inicial, ROI mínimo, régimen e impuestos.
4. **Contratar un VPS** y seguir `DESPLIEGUE.md` (30–45 min). Cambiar todas las claves del `.env`.
5. **Cargar 20–50 comparables** (precios pedidos y, cuando tengas, ventas reales) por modelo para que el tasador tenga confianza.
6. **Registrar una app en Mercado Libre** y hacer la prueba de 10 minutos de búsqueda con tu token (si da 403, se documenta y se cierra).

## 7. Siguiente incremento propuesto (Incremento 2)

1. **Carga de fotos en el panel** (archivos reales, hash, autenticidad) + inspector visual real con Anthropic sobre esas fotos.
2. **WhatsApp Business Cloud API inbound-first**: webhook de mensajes entrantes → conversación → agente → aprobación →
   envío dentro de la ventana de 24 h; plantillas utility con opt-in registrado en el CRM.
3. **Mercado Libre Preguntas API** sobre ítems propios (webhook → borrador → aprobación → respuesta) y publicación por API
   cuando esté el paquete.
4. **Tasador v2**: ajuste por kilómetros y versión, pesos por recencia, calibración automática de
   `listing_to_sale_discount` con ventas reales; precisión del tasador en el panel.
5. **Timeline de consultas por publicación** para que precios dinámicos use datos reales por semana.
6. **Operación**: alertas de salud (healthchecks externos), backups a almacenamiento externo (rclone), 2FA en el panel.
