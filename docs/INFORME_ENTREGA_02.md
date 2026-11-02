# Informe de entrega — Incremento 2 (2 de noviembre de 2026)

**Radar inteligente, detección de oportunidades y control operativo.** Construido sobre el Incremento 1 sin rehacer
nada: los 79 tests anteriores siguen (3 se actualizaron porque la semántica del radar cambió a propósito) y se
agregaron 44 → **123 tests, 123/123 en SQLite y en PostgreSQL 16**, cobertura 90 %.

## 1. Auditoría previa

Se verificó el estado del Incremento 1 (79/79), las 12 áreas, las reglas YAML, la migración 001 y el esquema de 31
tablas antes de tocar código. Decisión de diseño clave: el YAML conserva las **fórmulas** (cómo se calcula) y el nuevo
Centro de Control guarda los **parámetros operativos** en la base (capital, límites, estrategia, agentes, presupuesto de
IA). Donde ambos se superponen, manda la base; el hash de reglas pasa a combinar YAML + configuración, así cada tasación
y decisión registra exactamente con qué parámetros se tomó.

## 2. Qué se implementó (y está probado)

| Entregable del brief | Estado | Dónde |
|---|---|---|
| 4. **Centro de Control** editable y persistente (capital, búsqueda, agentes, costos de IA), auditado, aplicado por API y worker | ✅ | `mototrade/control/`, página "Centro de Control", `PUT /control/{sección}` |
| 4. Intención configurada vs. **estado efectivo** ("búsqueda habilitada pero pausada por capital") | ✅ | `OperationsGate`, tabla en el panel, `/status` y `/control/functions` |
| 5. **Gestión por capital e inventario**: capital libre = caja − reserva − compras pendientes (con colchón); máximo de motos configurable (2 por defecto, no fijo en código); pausa de búsqueda y de agente comprador; desactivación manual que no se reactiva sola | ✅ | `modules/finance/capital.py`, `control/gate.py`, tests `test_control_capital.py` |
| 2. **Detector de anomalías**: comparables equivalentes (misma versión si alcanza; año ±1), ajuste por km (±2 %/10.000 km, tope 15 %) y por tipo de vendedor (concesionaria +5 %), mediana/percentiles/IQR, descuento pedido-contra-pedido, clasificación 15/25 % configurable, riesgos (precio sospechoso, pocos comparables, km alto, versiones mezcladas, dispersión, sin fotos, texto sospechoso, bajadas repetidas, antigüedad, tope por unidad, presupuesto) | ✅ | `modules/anomalies/`, 20 tests |
| 3. **Modo A (publicada) y Modo B (negociable)** con oferta inicial, objetivo 15–25 %, máximo absoluto y escenarios conservador/central/adverso; nunca afirma que el vendedor aceptará | ✅ | `AnomalyDetector.analyze`, integrado en `RadarService` y en cada `Deal` (`analysis`, `opportunity_class`, `opportunity_mode`, `discount_pct`, `risk_flags`) |
| 7. **Optimización de IA**: filtros deterministas primero; registro `ai_usage` con costo estimado por modelo; límites mensual/diario y cupos; caché por entrada idéntica; al agotarse se frenan redacción/visión/análisis pero no inventario, finanzas ni conversaciones | ✅ | `modules/ai_budget/`, `MeteredLLM`, gate `ai_calls`/`visual_inspection` |
| 8. **Carga real de fotos** (validación, recompresión a ≤1600 px JPEG, SHA-256, dedupe) en panel, API y Telegram; almacenamiento local o Supabase Storage | ✅ | `modules/photos/`, `POST /listings/{id}/photos` |
| 8. **Inspector** sobre fotos cargadas, con caché, cupo mensual, hallazgos observación/hipótesis, **solicitud de cotización automática** por desperfecto (reparación "suspected"), confirmar/descartar; comparación reparar vs. vender sin reparar; costos afectan automáticamente la compra máxima | ✅ (real con API key; simulado sin ella) | `modules/inspection/`, `modules/parts/compare_repair_vs_as_is` |
| 6. **Fuentes**: ficha por fuente (acceso, API, cobertura, datos, restricciones, costo, frecuencia, estado); conectores independientes; **importación asistida por Telegram** (`/moto`, `/fotos`) con offset persistido | ✅ | `docs/INTEGRACIONES.md`, `integrations/telegram_inbox.py` |
| 9. Negociador conservado; agentes gobernados por el Centro de Control; ofertas justificadas con costos presupuestados reales | ✅ | `NegotiationService` (puerta operativa, nota de costos) |
| 10. **Nube gratuita**: Streamlit Cloud + Supabase + GitHub Actions + Telegram, restricciones verificadas, workflows `worker.yml` (cada 30 min) y `keepalive.yml` (latido + backup semanal), puente de `st.secrets`; Docker intacto | ✅ documentado y preparado; no ejecutado en la nube desde este entorno | `docs/NUBE_GRATUITA.md`, `.github/workflows/`, `.streamlit/secrets.toml.example` |
| 11. **Dashboard de oportunidades**: tarjetas con foto, datos, pedido, comparable, descuento, venta rápida, oferta inicial, compra máxima, gastos, ganancia, ROI, confianza, riesgos, fuente/enlace, estado; filtros y orden por descuento/ganancia/rentabilidad/riesgo/confianza/capital; alerta de extraordinarias; nunca "compra recomendada" | ✅ | `ui/views/radar.py`, Dashboard |
| 2. **Migraciones**: 002 (app_settings, ai_usage, columnas nuevas) con `server_default` para bases con datos | ✅ verificada en PostgreSQL con filas previas y en SQLite | `alembic/versions/20261102_102731bb4fc7_*.py` |

## 3. Pruebas obligatorias del brief → test que las cubre

| Caso | Test |
|---|---|
| Descuentos 15 %, 20 %, 25 % | `test_anomalies.py::test_discount_classification_15_20_25`, `::test_thresholds_are_configurable` |
| Precios anormalmente bajos | `::test_abnormally_low_price_flags_scam` |
| Versiones distintas del mismo modelo | `::test_version_matching_prefers_same_version` |
| Kilometraje elevado | `::test_km_adjustment_and_high_km_flag` |
| Comparables insuficientes | `::test_insufficient_comparables`, `::test_sold_comparables_do_not_mix_with_asking` |
| Anuncios duplicados | `::test_duplicate_listings_do_not_create_two_deals` (+ `test_radar_finance.py`) |
| Reparaciones imprevistas | `::test_unexpected_repairs_lower_max_purchase`, `test_photos_inbox.py::test_inspection_uses_stored_photos_and_caches` |
| Escenarios adversos de venta | `::test_adverse_sale_scenarios_present` (+ `test_valuation.py`) |
| Capital insuficiente | `test_control_capital.py::test_capital_insufficient_blocks_search`, `test_anomalies.py::test_capital_insufficient_marks_exceeds_budget_and_no_mode`, `test_negotiation.py::test_buyer_agent_paused_without_capital_or_capacity` |
| Inventario completo | `test_control_capital.py::test_inventory_full_pauses_search_and_buyer`, `test_worker_integrations.py::test_worker_suspends_and_recovers_with_capital_and_inventory` |
| Búsqueda desactivada manualmente | `test_control_capital.py::test_manual_disable_does_not_reactivate` (+ worker) |
| Presupuestos máximos de IA | `test_control_capital.py::test_ai_budget_limits_and_cache`, `::test_visual_quota`, `::test_fake_llm_is_free_and_not_limited` |
| Negociaciones que superen límites | `test_negotiation.py` (límite autorizado, rondas, kill switch, LLM que inventa montos) |
| Suspensión y recuperación de tareas programadas | `test_worker_integrations.py::test_worker_suspends_and_recovers_with_capital_and_inventory` |
| Persistencia de configuración | `test_control_capital.py::test_settings_persist_validate_and_audit`, `::test_corrupt_setting_row_is_ignored`, `test_ui.py::test_control_center_save_from_ui` |
| Integridad de auditoría | `test_core.py::test_audit_chain_detects_tampering`, `verify-audit` en el workflow |

Además: recorrido real por CLI sobre PostgreSQL (migrar → bootstrap → importar 4 anuncios → worker: 2 extraordinarias
detectadas, 1 anuncio sospechoso con riesgo alto, concesionaria descartada, modelo sin comparables marcado insuficiente →
auditoría íntegra), `docker compose config` válido, workflows YAML válidos.

## 4. Hallazgo de negocio importante (leer)

Con el modelo de costos e impuestos **por defecto** (Responsable Inscripto, IVA sobre margen, IIBB, transferencia,
venta rápida −8 %, contingencia), una moto necesita estar **~30 % por debajo del mercado** para dejar 10 % de ROI sobre
el costo total. Un descuento del 15–25 % **no alcanza** con esos supuestos: el detector lo informa (clase "interesante"
pero "ni con 25 % de rebaja cierra"), en vez de inflar la oportunidad. Con una estructura liviana (monotributo, base
"mercado" en vez de "venta rápida") los umbrales del brief sí funcionan (está probado en `_light()` de
`test_anomalies.py`). Esto es una decisión tuya con el contador: ajustar `taxes.*` y `valuation.deal_basis` en
`business_rules.yaml` o aceptar que el negocio exige descuentos mayores.

## 5. Qué sigue simulado o manual

- Búsqueda automática en Marketplace / RosarioGarage / Mercado Libre: **no existe** (ver ficha por fuente); la carga
  es asistida (Telegram en 30 segundos, formulario, CSV).
- Envío automático de mensajes a contrapartes: desactivado; no hay canal autorizado.
- Inspector visual y redacción con IA: simulados sin `MOTOTRADE_ANTHROPIC_API_KEY`; reales con ella (no se pudo
  llamar a la API desde este entorno; el cliente es el oficial y la medición de tokens está probada con dobles).
- Búsqueda de repuestos: manual (sin fuente autorizada); el estado efectivo lo dice.
- Nube gratuita: preparada y documentada; la primera ejecución real en Streamlit Cloud / Supabase / GitHub Actions
  la hacés vos siguiendo `NUBE_GRATUITA.md` (≈45 min).
- Telegram inbox: probado con transporte simulado; requiere token real.
- Radio desde Rosario: parámetro informativo (sin geocodificación todavía; se usa la lista de localidades).

## 6. Riesgos y limitaciones

- Calibración: `listing_to_sale_discount`, `km_adjustment_pct_per_10k`, `dealer_premium_pct` son supuestos hasta
  tener ventas reales; cargar comparables `sold` mejora la base y la confianza.
- Consultas por publicación siguen siendo un contador manual.
- GitHub Actions: cuota ≈2.000 min/mes en repo privado (cada 30 min con corridas de ≤1 min); si la instalación de
  dependencias tarda más, bajar a cada 60 min o usar repo público. Supabase se pausa a los 7 días sin actividad: el
  worker lo evita mientras corra.
- Streamlit Cloud duerme a las 12 h sin tráfico: es un panel interno, se despierta con un clic.
- Corregido durante el desarrollo: la puerta operativa bloqueaba al comprador en tests sin capital (comportamiento
  correcto; tests ajustados); columnas NOT NULL de la migración 002 sin `server_default` (agregados).

## 7. Acciones que requieren tu intervención

1. Decidir con el contador la estructura impositiva y la base de cálculo (punto 4) y cargarla en `business_rules.yaml`.
2. Fijar en el Centro de Control: capital total, reserva, máximo de motos, precio máximo por unidad, presupuesto de IA.
3. Crear el bot de Telegram y cargar token/chat_id; probar `/moto` con una foto.
4. Crear Supabase + Streamlit Cloud + secrets de GitHub según `NUBE_GRATUITA.md`.
5. Cargar 20–50 comparables por modelo (pedidos y, cuando haya, ventas reales), con versión y tipo de vendedor.

## 8. Incremento 3 propuesto

1. Priorización automática: análisis profundo con IA (texto + fotos) solo para las N mejores oportunidades del día,
   dentro del presupuesto; resumen diario por Telegram.
2. Geocodificación simple de localidades para el radio desde Rosario.
3. Timeline de consultas por publicación y calibración automática de `listing_to_sale_discount` con ventas reales.
4. WhatsApp Business Cloud API inbound-first (opt-in) y Preguntas API de ML sobre ítems propios.
5. Prueba de 10 minutos con app propia de ML para cerrar (o abrir) la búsqueda de ítems ajenos.
