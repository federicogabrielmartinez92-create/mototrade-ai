"""Adaptadores de fuentes de anuncios.

Contrato: cada adaptador devuelve `RawListing`s normalizados. El servicio de radar
se encarga de deduplicar, detectar cambios de precio y puntuar.

Estado real de cada fuente (ver docs/INTEGRACIONES.md):
- ManualSource / CsvSource: FUNCIONAN (carga por el operador).
- MercadoLibreSource: INTERFAZ + modo simulado con fixtures. La llamada real a la API
  oficial queda pendiente de credenciales OAuth y de confirmar el alcance permitido.
- Facebook Marketplace y RosarioGarage: NO hay adaptador automático. No se hace scraping.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Protocol

from mototrade.core.money import D


@dataclass(frozen=True)
class RawListing:
    source: str
    make: str
    model: str
    asking_price: Decimal
    external_id: str | None = None
    source_url: str | None = None
    version: str = ""
    year: int | None = None
    kilometers: int | None = None
    location: str | None = None
    title: str = ""
    description: str = ""
    seller_alias: str | None = None
    photos: list[str] = field(default_factory=list)
    observed_at: datetime | None = None
    seller_type: str = "unknown"          # particular | dealer | unknown
    condition_declared: str | None = None
    published_at: datetime | None = None
    raw: dict = field(default_factory=dict)


class ListingSourceAdapter(Protocol):
    name: str
    is_simulated: bool

    def fetch(self) -> Iterable[RawListing]: ...


class ManualSource:
    """Un anuncio cargado a mano desde el panel/API."""

    name = "manual"
    is_simulated = False

    def __init__(self, listings: Iterable[RawListing]):
        self._items = list(listings)

    def fetch(self) -> Iterable[RawListing]:
        return list(self._items)


def _to_int(v) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(str(v).replace(".", "").replace(",", ".")))
    except ValueError:
        return None


def _to_dt(v) -> datetime | None:
    if not v or not str(v).strip():
        return None
    try:
        dt = datetime.fromisoformat(str(v).strip())
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class CsvSource:
    """Importación por CSV. Columnas: make,model,year,kilometers,asking_price[,version,location,url,external_id,
    title,description,seller_alias,photos (separadas por |),seller_type (particular|dealer),condition,published_at (ISO)]."""

    name = "csv_import"
    is_simulated = False
    REQUIRED = ("make", "model", "asking_price")

    def __init__(self, content: str | bytes, source_label: str = "csv_import"):
        self._content = content.decode("utf-8-sig") if isinstance(content, bytes) else content
        self.name = source_label

    def fetch(self) -> Iterable[RawListing]:
        reader = csv.DictReader(io.StringIO(self._content))
        missing = [c for c in self.REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV sin columnas obligatorias: {missing}")
        out: list[RawListing] = []
        for i, row in enumerate(reader, start=2):
            price = _to_int(row.get("asking_price"))
            if price is None or price <= 0:
                raise ValueError(f"Fila {i}: asking_price inválido")
            photos = [p.strip() for p in (row.get("photos") or "").split("|") if p.strip()]
            out.append(
                RawListing(
                    source=self.name, make=row["make"].strip(), model=row["model"].strip(),
                    version=(row.get("version") or "").strip(), year=_to_int(row.get("year")),
                    kilometers=_to_int(row.get("kilometers")), asking_price=D(price),
                    external_id=(row.get("external_id") or "").strip() or None,
                    source_url=(row.get("url") or "").strip() or None, location=(row.get("location") or "").strip() or None,
                    title=(row.get("title") or "").strip(), description=(row.get("description") or "").strip(),
                    seller_alias=(row.get("seller_alias") or "").strip() or None, photos=photos, raw=dict(row),
                    seller_type=(row.get("seller_type") or "unknown").strip().lower() or "unknown",
                    condition_declared=(row.get("condition") or "").strip().lower() or None,
                    published_at=_to_dt(row.get("published_at")),
                )
            )
        return out


class MercadoLibreSource:
    """Adaptador para la API oficial de Mercado Libre (MLA).

    ESTADO: SIMULADO. `fetch()` devuelve los fixtures pasados al constructor. La
    implementación real requiere:
      1. Crear una aplicación en developers.mercadolibre.com.ar y obtener client_id/secret.
      2. Flujo OAuth 2.0 (authorization code + refresh token).
      3. Confirmar que el endpoint de búsqueda devuelva ítems de terceros para el
         token obtenido (desde 2024 la búsqueda pública está restringida).
      4. Respetar rate limits y términos de uso.
    `parse_item()` sí está implementado y probado contra la estructura pública de
    un ítem de ML, para que el salto a la API real sea solo "pegar el cliente HTTP".
    """

    name = "mercadolibre"
    is_simulated = True

    def __init__(self, fixture_items: Iterable[dict] | None = None, credentials_present: bool = False):
        self._fixtures = list(fixture_items or [])
        self.credentials_present = credentials_present

    @staticmethod
    def parse_item(item: dict) -> RawListing:
        attrs = {a.get("id"): a.get("value_name") for a in item.get("attributes", []) if a.get("id")}
        loc = item.get("location") or {}
        city = (loc.get("city") or {}).get("name") or (item.get("seller_address") or {}).get("city", {}).get("name")
        pics = [p.get("secure_url") or p.get("url") for p in item.get("pictures", []) if p.get("url") or p.get("secure_url")]
        if not pics and item.get("thumbnail"):
            pics = [item["thumbnail"]]
        return RawListing(
            source="mercadolibre",
            external_id=str(item.get("id")),
            source_url=item.get("permalink"),
            make=attrs.get("BRAND") or "",
            model=attrs.get("MODEL") or "",
            version=attrs.get("TRIM") or attrs.get("VERSION") or "",
            year=_to_int(attrs.get("VEHICLE_YEAR")),
            kilometers=_to_int((attrs.get("KILOMETERS") or "").replace("km", "")),
            asking_price=D(item.get("price") or 0),
            location=city,
            title=item.get("title") or "",
            description=item.get("description") or "",
            seller_alias=str((item.get("seller") or {}).get("nickname") or "") or None,
            photos=pics,
            observed_at=datetime.now(timezone.utc),
            raw={"currency_id": item.get("currency_id"), "status": item.get("status")},
        )

    def fetch(self) -> Iterable[RawListing]:
        return [self.parse_item(i) for i in self._fixtures]
