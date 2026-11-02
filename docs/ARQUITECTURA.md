# Arquitectura de MotoTrade AI

## Principios

1. **Una sola fuente de datos** (PostgreSQL) compartida por los 12 módulos. No hay doce aplicaciones.
2. **La IA propone, el código decide, el humano aprueba.** Los montos, límites y fórmulas viven en código y en
   `config/business_rules.yaml` (inmutable en memoria, con hash). El LLM solo interpreta texto y redacta.
3. **Nada con efecto externo o financiero sin aprobación humana** (cola `approval_requests`), con roles mínimos por tipo.
4. **Todo auditado** (`audit_log` append-only con cadena de hashes SHA-256: cualquier edición/borrado se detecta).
5. **Servicios independientes**: API, worker autónomo y panel corren en procesos/contenedores separados.
6. **Honestidad operativa**: cada integración sin credenciales o sin vía legítima se marca "simulado/manual" en pantalla
   y en la base (`is_simulated`), nunca se presenta como funcionando.

## Vista de componentes

```
                ┌──────────────────────────── VPS (docker compose) ────────────────────────────┐
                │                                                                             │
 Operador ──────┤  ui (Streamlit :8501) ──┐                                                   │
 (móvil/PC)     │                         ├──▶ mototrade (paquete Python: servicios + reglas) │
 Scripts/webhooks ─ api (FastAPI :8000) ──┤                 │                                  │
                │                         │                 ▼                                  │
                │  worker (jobs c/5 min) ─┘          PostgreSQL 16 (db)                       │
                │       │                                   ▲                                  │
                │       └──▶ Telegram (único canal saliente activo)     backup (pg_dump diario)│
                └─────────────────────────────────────────────────────────────────────────────┘
```

## Estructura del código

```
mototrade/
  config/      settings.py (env/secretos)  rules.py (business_rules.yaml validado + hash)
  db/          base.py (engine/sesiones)  models.py (31 tablas)        alembic/ (migraciones)
  core/        enums, money (Decimal), audit (hash chain), approvals, kill_switch, outbound, untrusted
  security/    auth (Argon2id, tokens HMAC, roles admin/operator/viewer)
  modules/
    radar/        1. ingestión (manual, CSV, adaptador ML), dedup, cambios de precio, scoring de oportunidades
    valuation/    2. comparables, estimación de mercado con confianza, matemática del negocio (15 salidas), escenarios
    inspection/   3. inspector visual (interfaz + fake + Anthropic visión), hallazgos observación/hipótesis
    parts/        4. reparaciones, presupuestos, comparar reparar vs. sustituir, costos para el tasador
    documents/    5. checklist documental (bloquea compra), evidencia, estados
    negotiation/  6. estrategia determinista comprador/vendedor, guardia de políticas, agente (LLM) → borradores
    logistics/    7. visitas / talleres
    publications/ 8. generación de avisos con datos reales, aprobación, publicación manual, consultas
    crm/          9. contactos (opt-in / no contactar), pipeline, atención pendiente
    finance/     10. libro de capital, compras/ventas con aprobación, gastos, P&L por unidad
    pricing/     11. precios dinámicos (días en stock, consultas) → aprobación PRICE_CHANGE
    tradein/     12. permutas: valor de toma, diferencia, rentabilidad combinada
    analytics/   13. KPIs, rendimiento por modelo, precisión del tasador
  integrations/  llm.py (Fake + Anthropic), telegram.py
  worker/        jobs.py (idempotentes, registrados en job_runs), scheduler.py
  api/           FastAPI (auth por API key o token de sesión)
  cli.py         init-db, bootstrap, create-admin, worker, check, verify-audit, import-csv
ui/              Streamlit multipágina con login y 14 secciones
```

## Incremento 2: capas nuevas

```
mototrade/control/      Centro de Control: settings_model (capital, búsqueda, agentes, IA), service (persistencia
                        auditada en app_settings + reglas efectivas con hash combinado), gate (intención vs. efectivo)
mototrade/modules/anomalies/  Detector de anomalías: comparables ajustados (km, versión, vendedor), descuento
                        pedido-contra-pedido, clasificación configurable, modo A/B, riesgos, puntaje
mototrade/modules/ai_budget/  Registro ai_usage, costos estimados por modelo, límites mensual/diario, caché (MeteredLLM)
mototrade/modules/photos/     Carga real de fotos (validación, recompresión, SHA-256), disco local o Supabase Storage
mototrade/modules/finance/capital.py  Posición de capital: reserva, compras pendientes reservadas, unidades activas
mototrade/integrations/telegram_inbox.py  Importación asistida por Telegram (/moto, /fotos) con offset persistido
```

Orden de evaluación de un anuncio (barato primero, IA al final y solo si hace falta):
1. Filtros deterministas: marca/modelo excluidos, duplicados (fingerprint), capital libre y capacidad, precio vs. tope.
2. Comparables equivalentes → referencia robusta → descuento → clase.
3. Matemática del tasador al precio pedido y al objetivo (modo A/B) → ¿oportunidad real?
4. Riesgos del anuncio (km, versión, fotos, texto sospechoso, bajadas de precio, antigüedad).
5. Solo si el operador lo pide (o en incrementos futuros, automáticamente para las mejores N): IA de texto/visión,
   con presupuesto, cupo y caché.

## Flujo comercial y dónde interviene el humano

```
anuncio (manual/CSV) → radar.score → Deal[opportunity] → tasación → [ADMIN autoriza precio máximo]
  → conversación de compra → agente propone borrador → [OPERADOR aprueba] → operador envía por el canal
  → contraparte responde (se registra como texto NO confiable) → ... → agente: "aceptar sujeto a aprobación"
  → [ADMIN aprueba ACCEPT_PURCHASE] → checklist documental OK → finanzas.register_purchase → InventoryItem
  → reparaciones/presupuestos → [ADMIN fija precio de lista y mínimo] → publicación (aprobación) → venta
  → agente vendedor (nunca < mínimo) → [ADMIN aprueba ACCEPT_SALE] → finanzas.register_sale → P&L → analítica
```

## Matemática del negocio (módulo valuation)

Entradas: comparables (precio PEDIDO `asking` o VENTA `sold`), reglas, costos específicos.

1. **Precio de mercado P_m**: mediana de ventas efectivas si hay ≥ `min_comparables`; si no, mediana de precios pedidos
   × (1 − `listing_to_sale_discount`), con `basis = asking_adjusted`. Nunca se confunden.
2. **Venta rápida** P_q = P_m × (1 − `quick_sale_discount`). **Publicación** P_l = P_m × (1 + `publish_markup`).
3. **Base del cálculo** S = P_q (conservador) o P_m según `deal_basis`.
4. **Costos C(p)** para un precio de compra p: repuestos + mano de obra + envío + traslado + preparación + marketing
   + transferencia(p) [1 % con mínimo + mitad de sellos + fijos] + imprevistos max(3 % p, mínimo) + impuestos(S, p)
   [IVA sobre margen + IIBB sobre max(margen, 10 % S) + otros].
5. **ROI(p)** = (S − p − C(p)) / (p + C(p)).
6. **Compra máxima** P_max = mayor múltiplo de `offer_rounding` con ROI(p) ≥ `minimum_roi` (búsqueda binaria; ROI es
   monótona decreciente en p). Test: comprando a P_max el ROI cumple; a P_max + 10.000 no.
7. **Oferta inicial** = ⌊P_max × `opening_offer_pct_of_max`⌋ redondeada.
8. **Escenarios**: base (S), mercado (P_m), adverso (S × (1 − 5 %)), severo (S × (1 − 12 %) y costos +25 %).
9. **Confianza** = 0,45·f(n) + 0,30·g(dispersión IQR/mediana) + 0,25·h(antigüedad) (+0,10 si base `sold`); etiqueta
   alta ≥ 0,70, media ≥ 0,40, baja < 0,40 o n < `min_comparables` (revisión humana obligatoria).

Cada tasación persiste: fuentes (ids de comparables), fecha, confianza, `rules_hash`.

## Negociador

- **Interpretación** (LLM o fake): intención + monto extraído del texto envuelto como dato no confiable.
- **Estrategia determinista**: comprador arranca en 88 % del máximo autorizado y sube 3 % por ronda, nunca supera el
  máximo; si la contraparte pide ≤ máximo → `accept_pending_approval` (crea ACCEPT_PURCHASE para el admin). Vendedor
  simétrico con precio de lista y mínimo autorizado.
- **Guardia**: kill switch, agente habilitado, intenciones prohibidas (pagar, reservar, cerrar), rondas máximas, tope
  diario de mensajes, horario de silencio (no bloquea el borrador, bloquea el envío), límite autorizado.
- **Redacción** (LLM o plantilla): recibe intención y monto ya decididos; si el texto contiene otro monto, se reemplaza
  por la plantilla segura y queda registrado.
- **Divulgación**: el primer mensaje aclara que es un asistente automático y quién confirma los cierres.

## Seguridad

- Contraseñas Argon2id; tokens HMAC-SHA256 con expiración; roles admin/operator/viewer; API key para servicios.
- Secretos solo por variables de entorno (`.env` fuera del repo).
- Texto de terceros: normalización NFKC, recorte, detección de patrones de prompt injection (flags en la fila), envoltura
  explícita para el LLM. La defensa principal: el LLM no ejecuta acciones; la capa de políticas valida cada propuesta.
- Idempotencia: `idempotency_key` único en aprobaciones y acciones salientes; `UNIQUE(source, external_id)` en anuncios.
- Reintentos acotados (`MAX_ATTEMPTS`) en acciones salientes; el worker nunca muere por un job (errores en `job_runs`).
- Kill switch global y pausa del worker, consultados antes de cada acción.
- Base no expuesta a internet; API y panel solo en localhost detrás de un reverse proxy con TLS.
- Backups diarios (`pg_dump`) con rotación de 14 días; copiarlos fuera del VPS.

## Limitaciones conocidas (versión 0.1)

- No hay búsqueda automática de anuncios ni envío automático a contrapartes (ver `INTEGRACIONES.md`).
- La estimación de mercado no ajusta por kilómetros/versión: filtra por marca, modelo y año ±1 (±2 si faltan datos).
- El tasador necesita comparables cargados a mano; con < 3 la confianza es baja.
- Las consultas por publicación son un contador manual (sin timeline).
- Las fotos se referencian por URL/path; no hay carga de archivos en el panel todavía.
- El modelo impositivo es una aproximación parametrizada; lo define el contador.
