"""Aritmética monetaria con Decimal. Nunca float para dinero."""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")


def D(value) -> Decimal:
    """Convierte int/str/float/Decimal a Decimal de forma segura."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def money(value) -> Decimal:
    """Redondea a centavos (half-up)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def round_down_to(value, step) -> Decimal:
    """Redondea hacia abajo a múltiplos de `step` (p. ej. ofertas a múltiplos de 10.000)."""
    value, step = D(value), D(step)
    if step <= 0:
        return money(value)
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def pct(value, fraction) -> Decimal:
    return money(D(value) * D(fraction))


def fmt_ars(value) -> str:
    """Formato argentino: $ 1.234.567"""
    v = D(value).quantize(ONE, rounding=ROUND_HALF_UP)
    sign = "-" if v < 0 else ""
    digits = f"{abs(int(v)):,}".replace(",", ".")
    return f"{sign}$ {digits}"
