# MotoTrade AI

Plataforma privada para gestionar y automatizar un negocio de compra y reventa de motos usadas (Rosario, Santa Fe).
**Versión 0.2 — incremento 2 (radar inteligente, Centro de Control, control de capital).** Motor de negocio en Python, API FastAPI, panel Streamlit, PostgreSQL, worker autónomo,
Docker. 12 módulos sobre una única base de datos.

> Regla de oro del sistema: **la IA propone, el código decide dentro de tus límites, vos aprobás.** Ningún agente paga,
> reserva, acepta una compra ni cierra una venta. Todo queda auditado con cadena de hashes.

## Qué funciona de verdad (verificado con 123 tests, SQLite y PostgreSQL 16)

| Módulo | Estado | Detalle |
|---|---|---|
| 1 Tasador | ✅ real | Comparables (asking ≠ sold), precio de mercado con confianza, venta rápida, publicación, oferta inicial, **compra máxima** (garantiza ROI mínimo), costos (transferencia e impuestos como función del precio), escenarios adverso/severo, fuentes, fecha, hash de reglas |
| 2 Inspector visual | 🧪 simulado / ✅ real con API key | Interfaz + hallazgos observación/hipótesis + confirmación humana. Sin API key de Anthropic lee palabras clave; con API key analiza fotos |
| 3 Repuestos | ✅ real (carga manual) | Reparaciones, presupuestos con fuente/fecha, reparar vs. sustituir, costos al tasador. Sin búsqueda automática (no hay API) |
| 4 Auditor documental | ✅ real (sin verificación oficial) | Checklist de transferencia (DNRPA/Santa Fe), evidencia, bloquea la compra. No existe API oficial |
| 5 Negociador | ✅ real (borradores) | Comprador/vendedor con estrategia determinista acotada por límites autorizados, guardia de políticas, aprobación por mensaje, historial, takeover. Redacción con plantillas (fake) o Anthropic. **Envío a contrapartes: manual** |
| 6 Visitas y talleres | ✅ básico | Agenda interna, proveedores |
| 7 Publicaciones | ✅ real (publicación manual) | Aviso con datos reales, aprobación, registro de URL y consultas |
| 8 CRM | ✅ básico | Contactos con opt-in / no contactar, pipeline, conversaciones que esperan decisión |
| 9 Inventario y finanzas | ✅ real | Libro de capital, compra/venta solo con aprobación admin, gastos, resultado por moto, impuestos configurables |
| 10 Precios dinámicos | ✅ real | Recomendaciones por días en stock y consultas, con pisos, vía aprobación |
| 11 Permutas | ✅ real | Valor de toma, diferencia en efectivo, rentabilidad combinada, veredicto |
| 12 Analítica | ✅ real | Márgenes, rotación, días, rendimiento por modelo, precisión del tasador |
| Radar + **Detector de anomalías** | ✅ análisis · 🧪 fuentes | Comparables ajustados (km/versión/vendedor), descuento 15/25 % configurable, modo publicada/negociable, riesgos, escenarios. Carga asistida: Telegram `/moto`, formulario, CSV. **No hay búsqueda automática** en ML/Marketplace/RosarioGarage (sin API autorizada, sin scraping) |
| **Centro de Control** | ✅ | Capital, exposición, búsqueda, agentes y presupuesto de IA editables desde el panel, persistidos y auditados; intención vs. estado efectivo |
| **Control de capital** | ✅ | Capital libre con reservas por compras pendientes, máximo de motos configurable, pausa automática de búsqueda/comprador |
| **Presupuesto de IA** | ✅ | Registro de uso y costo por llamada, límites mensual/diario, cupos, caché |
| **Fotos reales** | ✅ | Carga en panel/API/Telegram, recompresión, SHA-256, disco local o Supabase Storage |
| **Notificaciones** | ✅ | Centro de notificaciones persistente en la app (canal principal). Telegram: opcional, solo copia |
| Seguridad | ✅ | Login Argon2id, roles, API key, kill switch, auditoría encadenada, idempotencia, anti prompt-injection, backups |

Ver `docs/INTEGRACIONES.md` para el detalle de qué es posible, qué requiere permisos y qué no.

## Arranque rápido (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .
cp .env.example .env            # editar MOTOTRADE_ADMIN_PASSWORD; para probar: MOTOTRADE_DATABASE_URL=sqlite:///./mototrade.sqlite3
alembic upgrade head && python -m mototrade.cli bootstrap
streamlit run ui/app.py         # panel en http://localhost:8501
python -m mototrade.cli worker --once   # una pasada del worker
python -m pytest -q             # tests
```

**Despliegue paso a paso desde GitHub Actions (Supabase + Streamlit Cloud, sin VPS ni Telegram): `docs/DESPLIEGUE_GITHUB.md`.** Restricciones de la nube gratuita: `docs/NUBE_GRATUITA.md`. VPS con Docker: `docs/DESPLIEGUE.md`. Guía de uso diario: `docs/OPERACION.md`. Arquitectura y
fórmulas: `docs/ARQUITECTURA.md`. Informe de esta entrega: `docs/INFORME_ENTREGA_01.md`.

## Estructura

```
config/business_rules.yaml   reglas financieras (solo las edita el humano; hash en cada decisión)
mototrade/                   motor: config, control (Centro de Control), db (33 tablas), core, security, modules/ (12 + anomalías, IA, fotos), integrations, worker, api, cli
ui/                          panel Streamlit (login, 14 secciones)
alembic/                     migraciones
docker/ docker-compose.yml   db + api + worker + ui + backup
tests/                       123 tests (unitarios, integración, API, panel headless)
docs/                        arquitectura, integraciones, nube gratuita, operación, despliegue, informes 01 y 02
```
