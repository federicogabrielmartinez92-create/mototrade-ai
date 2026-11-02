# Integraciones: qué es posible hoy, qué requiere permisos, qué no

**Fecha de corte: 2 de noviembre de 2026.** Resultado de una investigación sobre documentación oficial y fuentes
secundarias. Donde no se pudo verificar, se dice. Esto define qué prometemos y qué no en el software.

## Matriz de viabilidad

| Integración | Clasificación | Qué se puede hacer hoy | Qué requiere | Riesgos / prohibiciones |
|---|---|---|---|---|
| **Mercado Libre – buscar motos de terceros por API** | **NO POSIBLE LEGÍTIMAMENTE HOY** | Nada confiable. La doc vigente (04/2025) solo documenta `/sites/MLA/search` por `seller_id`/`nickname`; el parámetro `q` desapareció. Reporte de terceros (10/2026): 403 en todas las variantes para apps de terceros. | Probar con app propia + token para confirmar y documentar. | Scraping prohibido por T&C Developers (cl. 7.6). |
| **ML – categorías/atributos** | POSIBLE AHORA | Leer árbol de categorías (`MLA1763` = Motos) y atributos (`BRAND`, `MODEL`, `VEHICLE_YEAR`, `TRIM`). | App registrada + access token. | Rate limit por Client ID, sin cifras públicas. |
| **ML – Preguntas sobre ítems propios** | POSIBLE AHORA | Recibir preguntas por webhook y responder por API. | App + OAuth del vendedor. | El contenido sigue políticas de ML. |
| **ML – contactar vendedores ajenos** | NO POSIBLE LEGÍTIMAMENTE HOY | Existe `POST /questions`, pero usarlo para prospección es abuso (cl. 7.8, 5.4.5). | — | Revocación de credenciales (cl. 14). |
| **ML – publicar motos por API** | REQUIERE PERMISOS/CREDENCIALES | `POST /items` en categoría vehículos. | Paquete "Silver" contratado con ejecutivo de cuenta (no por API). | Costos AR a verificar a mano. |
| **RosarioGarage** | NO POSIBLE (vía automática) | Publicar y contactar a mano. | Sin API, feed ni RSS. | Captcha + Cloudflare; scraping = riesgo legal (Ley 25.326) y bloqueo. |
| **Facebook Marketplace (listados)** | NO POSIBLE (API) | Carga manual desde perfil; Automotive Inventory Ads (pago). | Business Manager + catálogo (solo para ads). | ToS 3.2.3 prohíbe recolección automatizada. |
| **Facebook Marketplace (chats)** | NO POSIBLE | Nada por API: Messenger Platform solo cubre Páginas; "la persona debe iniciar". | — | Automatizar perfiles personales viola ToS. |
| **WhatsApp Cloud API** | REQUIERE PERMISOS/CREDENCIALES | Bot inbound + plantillas salientes **con opt-in**. | Business portfolio, número dedicado, verificación (250→2.000 destinatarios/día), plantillas aprobadas. | **Opt-in obligatorio**: no se puede escribir a vendedores que publicaron su número. Cobro por mensaje (AR: marketing US$0,0618; utility/service US$0,026; desde 10/2026 service se cobra pasados 1.000/mes). |
| **Telegram Bot API** | POSIBLE AHORA (**implementado**) | Alertas y consola interna del operador. | Token de @BotFather (gratis). | Bots no inician chats: el operador le escribe primero. |
| **Claude API (visión)** | POSIBLE AHORA (**implementado, requiere API key**) | Analizar fotos de motos (daños, consistencia con el aviso). | API key, pago por tokens (10 fotos ≈ US$0,001–0,03 según modelo). | 10 MB/imagen, 100 imágenes/request. |
| **DNRPA – informe de dominio / 08D** | REQUIERE TRÁMITE ONLINE (sin API) | Pedir informe online (www2.jus.gov.ar, 24 h por mail), precargar 08. | Identidad, arancel. | **No existe API pública.** |

## Fuentes de anuncios: ficha por fuente (incremento 2)

| Fuente | Acceso real | API / feed permitido | Cobertura Rosario | Datos disponibles | Restricciones | Costo | Frecuencia viable | Estado en MotoTrade |
|---|---|---|---|---|---|---|---|---|
| **Facebook Marketplace** | Solo navegación manual | Ninguno para listados ni chats (Messenger Platform cubre Páginas, no Marketplace) | Alta (es donde más particulares publican) | Título, precio, fotos, ubicación, descripción (a ojo) | ToS 3.2.3 prohíbe recolección automatizada | 0 (manual) / Automotive Inventory Ads es pago | Manual, cuando el operador mira | **Importación asistida**: `/moto` por Telegram, formulario, CSV. Conector automático: NO (no se promete) |
| **RosarioGarage** | Navegación manual; captcha + Cloudflare | Sin API, feed ni RSS; T&C no leídos (verificar) | Alta y local (~2.600 avisos de motos) | Marca, modelo, año, km, precio, agencia/particular, WhatsApp | Scraping = riesgo legal (Ley 25.326) y bloqueo | 0 | Manual | **Importación asistida**. Sin acuerdo comercial en esta etapa |
| **Mercado Libre** | App OAuth propia; búsqueda de ítems de terceros devuelve 403 | Preguntas/mensajes/publicación solo sobre ítems PROPIOS | Media (menos usados baratos que Marketplace) | Ítem completo (atributos, fotos, precio, vendedor) solo de ítems propios | T&C: sin scraping; volumen razonable; revocación | Publicar vehículos requiere paquete Silver (ejecutivo de cuenta) | Si algún día hay acceso: cada 30 min | Parser de ítems implementado y probado; **cliente HTTP pendiente de credenciales**. Carga manual hoy |
| **Telegram (propio)** | Bot propio | Bot API oficial | — | Lo que el operador manda: `/moto` + fotos | 24 h de retención de updates; 20 MB por foto | 0 | Cada 15–30 min (GitHub Actions) o continuo (VPS) | **Implementado y probado** |
| **CSV / formulario** | — | — | — | Todos los campos (incl. vendedor, estado, fecha de publicación) | — | 0 | Inmediato | **Implementado y probado** |
| Otros sitios (OLX/Autocosmos/Kavak) | A investigar | A investigar | Baja para motos usadas baratas | — | — | — | — | No evaluados en esta etapa |

**Conclusión honesta**: hoy MotoTrade **no busca automáticamente** en Marketplace ni RosarioGarage, y no puede
buscar anuncios ajenos en Mercado Libre. Lo que sí hace: convertir en 30 segundos (un mensaje de Telegram con foto)
cada anuncio que vos ves en un análisis completo con comparables, descuento, riesgos y objetivo de compra.

## Qué implementa el software en consecuencia

- **Radar**: carga manual y CSV (funciona). Adaptador `MercadoLibreSource` con el *parser* de ítems implementado y probado
  sobre la estructura pública de un ítem, pero **sin cliente HTTP**: queda listo para enchufar si una app propia logra
  acceso. No hay ni habrá scraping.
- **Negociador**: genera borradores y los envía a aprobación. El único canal de salida implementado es **Telegram al
  operador**. "Canal manual" = vos copiás el texto aprobado y lo mandás por donde corresponda (WhatsApp personal, chat de
  ML, Marketplace). WhatsApp Business queda como integración futura **solo inbound / con opt-in**.
- **Publicaciones**: genera el aviso, pide aprobación y vos lo publicás a mano; registra URL y consultas.
- **Documental**: checklist + evidencia; **ninguna verificación es oficial**.
- **Inspector visual**: real con API key de Anthropic; simulado sin ella (marcado como tal en pantalla y en la base).

## Costos y reglas incorporados a `business_rules.yaml`

- Arancel registral transferencia moto: **1 % del valor**, mínimo **15 MRPA** (MRPA ≈ $1.050 al 1/9/2026, se actualiza
  cuatrimestralmente) → `transfer_registral_pct`, `transfer_registral_min`.
- Impuesto de sellos Santa Fe: **1,2 %** (cód. 14012), por mitades → `transfer_sellos_pct`, `transfer_sellos_buyer_share`.
- Verificación física (F.12) obligatoria para modelos 2004+ (confirmar corte con el Registro) → `transfer_verification_fixed`.
- Monotributo: tope de precio unitario **$716.840,77** (2026) → una moto usada típica lo excede → **Responsable Inscripto**;
  IVA régimen de bienes usados (art. 18 Ley IVA) aproximado como IVA sobre margen → `iva_margin_pct`.
- IIBB Santa Fe: base diferencia compra-venta, mínimo 10 % del precio de venta (CF art. 191 inc. j), alícuota 4,5 % →
  `iibb_pct`, `iibb_min_base_pct_of_sale`. **Confirmar con el contador** si la base diferencial aplica a un habitualista unipersonal.

## Fuentes principales

- ML Ítems y búsquedas: https://developers.mercadolibre.cl/en_us/items-and-searches
- ML rate limits: https://developers.mercadolibre.com.mx/en_us/rate-limit-429-error
- ML preguntas: https://developers.mercadolibre.com.co/en_us/manage-questions-and-answers
- ML mensajería: https://developers.mercadolibre.com.ar/en_us/what-is-messaging
- ML paquetes vehículos: https://developers.mercadolibre.cl/en_us/vehicles-manage-packages
- ML T&C developers: https://developers.mercadolibre.com.ar/en_us/mercado-libre-developer-terms-and-conditions
- Evidencia 403 búsqueda terceros (10/2026): https://github.com/icemint/Alquiler-bot/pull/3
- RosarioGarage: https://www.rosariogarage.com/Motos
- Meta recolección automatizada: https://developers.facebook.com/docs/development/terms-and-policies/automated-data-collection/
- Messenger Platform: https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview.md
- Automotive Inventory Ads: https://developers.facebook.com/docs/marketing-api/auto-ads/guides/catalog
- WhatsApp overview: https://developers.secure.facebook.com/docs/whatsapp/overview
- WhatsApp límites: https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits.md
- WhatsApp precios: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
- WhatsApp opt-in: https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in.md
- Telegram bots: https://core.telegram.org/bots · FAQ límites: https://core.telegram.org/bots/faq
- Claude visión: https://platform.claude.com/docs/en/build-with-claude/vision · precios: https://platform.claude.com/docs/es/about-claude/pricing
- DNRPA informe de dominio: https://www.argentina.gob.ar/solicitar-un-informe-de-dominio-del-automotor
- Arancel 1 % / Res. 412/2026: https://www.cira.org.ar/es/servicios/normativas-servicios/resolucion/resolucion-412-2026/
- MRPA Res. 308/2026: https://www.colegio-escribanos.org.ar/noticias/2026_07_07-Min-Jus-Res-308-26.pdf
- Sellos Santa Fe: https://www.santafe.gob.ar/index.php/web/content/download/258548/1361139/file/NOMENCLADOR%20SELLOS%20Y%20TRS%202025.pdf
- Monotributo topes 2026: https://www.iprofesional.com/impuestos/464455-arca-definio-el-monotributo-cuanto-se-paga-en-octubre-2026-y-cuales-son-nuevos-topes.amp
- IVA art. 18: https://leyes-ar.com/ley_de_impuesto_al_valor_agregado/18.htm
- IIBB Santa Fe CF: https://www.santafelegal.com.ar/cods/cf2.html

## Orden recomendado (90 días)

1. **Días 0–30**: Telegram (ya implementado: cargar token), Claude visión (cargar API key), carga manual asistida, registrar
   app en ML y hacer la prueba de 10 minutos de búsqueda con token propio; leer T&C de RosarioGarage.
2. **Días 30–60**: WhatsApp Cloud API inbound-first con plantillas utility y opt-in registrado en el CRM; ML Preguntas API
   sobre ítems propios (webhook → borrador → aprobación → respuesta).
3. **Días 60–90**: publicación en ML por API una vez contratado el paquete; alta impositiva y documentación de compra firmada
   por el consumidor final (crédito presunto art. 18).

**Explícitamente NO hacer**: scrapear ML, Facebook o RosarioGarage; contactar vendedores ajenos por `POST /questions` o
WhatsApp sin opt-in; bots sobre chats de Marketplace o perfiles personales.
