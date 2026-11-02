# Guía de operación (para el dueño del negocio, sin conocimientos técnicos)

## Qué hace el sistema por vos y qué te pide

El sistema **ordena, calcula, propone y avisa**. Vos **decidís**. Concretamente:

| El sistema hace solo | Te pide que decidas |
|---|---|
| Deduplica anuncios, detecta bajas de precio, puntúa oportunidades y te avisa por Telegram | Cargar los anuncios (a mano o CSV) hasta que exista una fuente autorizada |
| Calcula precio de mercado, venta rápida, compra máxima, oferta inicial, costos, escenarios | Autorizar el **precio máximo** de cada deal (solo admin) |
| Redacta el mensaje de negociación (sin pasarse del límite) | **Aprobar cada mensaje** antes de que salga, y enviarlo vos por el canal |
| Detecta cuándo la contraparte aceptó un precio dentro del límite | **Aprobar la compra** (solo admin) y registrar la compra |
| Mantiene el checklist de papeles y bloquea la compra si faltan | Marcar cada documento como verificado tras revisarlo |
| Compara presupuestos de reparación | Cargar los presupuestos con fuente y fecha |
| Genera el texto del aviso de venta | Aprobarlo y publicarlo a mano |
| Recomienda bajar precios por antigüedad/consultas | Aprobar cada cambio de precio |
| Lleva caja, costos, resultado por moto, KPIs | Aportes/retiros de capital y registrar ventas cerradas |

## Centro de Control (incremento 2)

Todo lo operativo se cambia desde el panel, sin tocar archivos: capital total, reserva, máximo de motos, precio máximo
por unidad, búsqueda automática, descuentos objetivo (15–25 %), rentabilidad mínima, agentes, presupuesto de IA. Cada
cambio queda auditado con "antes/después". La tabla **Intención vs. estado efectivo** te dice si una función que
habilitaste está realmente corriendo o por qué está pausada (p. ej. "capital libre insuficiente", "inventario
completo 2/2", "presupuesto de IA agotado"). Si desactivás algo a mano, no se reactiva solo.

## Cargar anuncios desde el celular (Telegram)

Cuando veas una moto en Marketplace, RosarioGarage o ML, mandale al bot:

    /moto Honda | Wave 110 | 2022 | 15000 | 2300000 | Rosario | https://link.del.anuncio | particular

con las fotos adjuntas (o después: `/fotos <id>` + fotos). En la siguiente pasada del worker (cada 30 min en la nube
gratuita, 5 min en VPS) el anuncio se analiza y recibís: descuento vs. comparables, clase (ordinaria / interesante /
🚨 extraordinaria), objetivo de compra y riesgos.

## Cómo leer una oportunidad

- **Descuento detectado**: pedido vs. mediana de comparables equivalentes (ajustados por km, versión y tipo de vendedor).
  15–25 % = interesante; > 25 % = extraordinaria: **también puede ser estafa, daño o error → verificar**.
- **Modo publicada**: ya está barata y cierra la rentabilidad al precio pedido. **Modo negociable**: a precio normal,
  cerraría si conseguís una rebaja de 15–25 %; el sistema propone oferta inicial y objetivo, pero **no afirma que el
  vendedor vaya a aceptar**.
- **Compra máxima**: el techo absoluto por rentabilidad y riesgo; el agente comprador jamás lo supera.
- Nada es "compra recomendada" hasta completar el checklist documental y la revisión mecánica.

## Rutina diaria (10 minutos)

1. Abrí el panel → **Dashboard**. Mirá "Pendientes de tu decisión".
2. **Aprobaciones**: aprobá o rechazá borradores de mensajes, cambios de precio, publicaciones.
3. Para cada mensaje aprobado: en la conversación aparece el texto en un recuadro → copialo, mandalo por WhatsApp/ML/Marketplace
   y tocá **"Ya lo envié → marcar enviado"**.
4. Cuando te respondan: pegá la respuesta en **"Pegar mensaje recibido"** y tocá **"Pedir propuesta al agente"**.
5. **Radar**: cargá los anuncios nuevos que viste y tocá **"Escanear oportunidades"** (el worker lo hace solo cada 5 minutos).

## Cuando algo no te gusta

- **Botón rojo 🛑** (Configuración de agentes): frena todo lo automático. Lo libera solo un admin.
- **"Tomar la conversación"**: el agente deja de proponer en esa conversación; escribís vos.
- **Rechazar** un borrador: queda registrado y el agente no insiste.

## Cambiar límites y fórmulas

Editá `config/business_rules.yaml` (ROI mínimo, capital, porcentajes de oferta, impuestos, modelos a vigilar) y reiniciá
los servicios (`docker compose restart`). El panel muestra la versión y el hash de las reglas en uso. **La IA no puede
tocar este archivo.**

## Qué está simulado hoy (y se ve en pantalla con 🧪)

- Búsqueda automática de anuncios en Mercado Libre / Marketplace / RosarioGarage.
- Envío automático de mensajes a compradores/vendedores (solo Telegram a vos está activo).
- Inspector visual sin API key de IA (lee palabras clave de la descripción, no analiza fotos).
- Publicación automática de avisos.
- Verificación oficial de dominio/prendas (no existe API; el sistema registra lo que vos verificás).

## Telegram (5 minutos de configuración)

1. En Telegram, hablá con `@BotFather` → `/newbot` → copiá el **token**.
2. Escribile un "hola" a tu bot nuevo (los bots no pueden iniciar la charla).
3. Para obtener tu `chat_id`: abrí `https://api.telegram.org/bot<TOKEN>/getUpdates` en el navegador y copiá `"chat":{"id":...}`.
4. Poné ambos valores en `.env` (`MOTOTRADE_TELEGRAM_BOT_TOKEN`, `MOTOTRADE_TELEGRAM_CHAT_ID`) y reiniciá.

## IA real (opcional)

Con una API key de Anthropic en `.env` (`MOTOTRADE_ANTHROPIC_API_KEY`, `MOTOTRADE_LLM_PROVIDER=anthropic`) el
negociador redacta con lenguaje natural y el inspector analiza fotos. Sin clave, todo sigue funcionando con plantillas.
