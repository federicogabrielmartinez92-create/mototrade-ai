"""CLI de operación: `python -m mototrade.cli <comando>` o `mototrade <comando>` si está instalado."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from mototrade.config import get_rules, get_settings
from mototrade.db import init_db, session_scope


def cmd_init_db(_):
    """Solo desarrollo/tests (create_all). En producción usar 'migrate'."""
    init_db()
    print("Tablas creadas/verificadas en", _redact(get_settings().database_url))


def cmd_create_admin(args):
    from mototrade.security import create_user

    with session_scope() as s:
        u = create_user(s, args.username, args.password, "admin", created_by="cli")
        print(f"Usuario admin '{u.username}' creado.")


def _alembic_config():
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


def _redact(url: str) -> str:
    """Nunca imprimir credenciales: deja solo host/base."""
    return url.split("@")[-1] if "@" in url else url.split("://")[0] + "://…"


def cmd_migrate(_):
    """Aplica las migraciones Alembic (alembic upgrade head). No inicia tareas comerciales."""
    from alembic import command

    from mototrade.db.diagnostics import diagnose_from_settings

    print("Base:", _redact(get_settings().database_url))
    d = diagnose_from_settings()
    if d.category not in ("ok", "schema_pending"):
        print(f"NO SE PUEDE MIGRAR [{d.category}]: {d.message}")
        for h in d.hints:
            print(f"  → {h}")
        sys.exit(1)
    command.upgrade(_alembic_config(), "head")
    cmd_db_status(_)


def cmd_db_check(_):
    """Diagnóstico de conexión clasificado y SEGURO (sin credenciales). Sale con 1 si no está OK."""
    from mototrade.db.diagnostics import diagnose_from_settings

    d = diagnose_from_settings()
    print(f"Conexión: {d.url.redacted or '—'}")
    print(f"Categoría: {d.category}")
    print(f"Detalle: {d.message}")
    if d.server_version:
        print(f"Servidor: {d.server_version}")
    if d.schema_current or d.schema_head:
        print(f"Esquema: aplicado={d.schema_current or 'ninguno'} · esperado={d.schema_head}")
    for h in d.hints:
        print(f"  → {h}")
    if not d.ok:
        sys.exit(1)


def cmd_db_status(_):
    """Compara la revisión aplicada con la última del repo. Sale con 1 si faltan migraciones o no conecta."""
    from mototrade.db.diagnostics import diagnose_from_settings

    d = diagnose_from_settings()
    print(f"Migraciones: aplicada={d.schema_current or 'ninguna'} · última={d.schema_head or '?'}")
    if d.category == "schema_pending":
        print("FALTAN MIGRACIONES: ejecutá 'mototrade migrate' (workflow setup-database).")
        sys.exit(1)
    if not d.ok:
        print(f"NO SE PUDO CONECTAR [{d.category}]: {d.message}")
        for h in d.hints:
            print(f"  → {h}")
        sys.exit(1)
    print("Esquema al día.")


def cmd_reset_password(args):
    """Resetea la contraseña de un usuario tomándola de MOTOTRADE_ADMIN_PASSWORD (secreto). Nunca la imprime."""
    from mototrade.security import AuthError, reset_password

    pwd = get_settings().admin_password.get_secret_value()
    if not pwd:
        print("Falta MOTOTRADE_ADMIN_PASSWORD (la nueva contraseña se toma de ese secreto).")
        sys.exit(1)
    try:
        with session_scope() as s:
            u = reset_password(s, args.username, pwd, by="cli")
        print(f"Contraseña de '{u.username}' actualizada.")
    except AuthError as e:
        print(str(e))
        sys.exit(1)


def cmd_bootstrap(args):
    """Crea el admin inicial (desde MOTOTRADE_ADMIN_PASSWORD) y el capital inicial. Requiere migraciones aplicadas.
    Idempotente: no toca nada si ya existe. Nunca imprime la contraseña."""
    from mototrade.control import ControlCenter
    from mototrade.modules.finance import FinanceService
    from mototrade.security import ensure_admin_from_settings

    with session_scope() as s:
        u = ensure_admin_from_settings(s)
        cc = ControlCenter(s)
        cc.ensure_defaults()
        cap = None if getattr(args, "no_capital", False) else FinanceService(s, cc.effective_rules()).ensure_initial_capital()
    has_pwd = bool(get_settings().admin_password.get_secret_value())
    print("Admin:", "creado" if u else ("ya existía un usuario" if has_pwd else "no creado: falta MOTOTRADE_ADMIN_PASSWORD"))
    print("Centro de Control: valores por defecto verificados")
    print("Capital inicial:", "registrado" if cap else "ya existía / omitido")


def cmd_worker(args):
    from mototrade.worker import Scheduler

    sched = Scheduler(args.interval)
    if args.once:
        print(json.dumps(sched.tick(), ensure_ascii=False, indent=2, default=str))
    else:
        sched.run_forever()


def cmd_check(_):
    from mototrade.core import audit

    s = get_settings()
    rules = get_rules()
    print("Entorno:", s.env, "· base:", _redact(s.database_url))
    print("Reglas:", rules.source_path, "hash", rules.rules_hash[:12], "versión", rules.version)
    print("Integraciones:")
    for k, v in s.integration_status().items():
        print(f"  - {k}: {v['estado']} [{v['modo']}]")
    for w in s.security_warnings():
        print("  ! ADVERTENCIA:", w)
    with session_scope() as sess:
        ok, broken = audit.verify_chain(sess)
        print("Cadena de auditoría:", "íntegra" if ok else f"ROTA en id {broken}")
        from mototrade.control import OperationsGate

        g = OperationsGate(sess)
        print("Estado operativo (Centro de Control):")
        for name, st in g.snapshot().items():
            print(f"  - {name}: {st.label}")
        pos = g.position
        print(f"Capital: caja {pos.ledger_balance:,.0f} · libre {pos.free_capital:,.0f} · motos {pos.units_active}/{pos.max_units}")


def cmd_verify_audit(_):
    from mototrade.core import audit

    with session_scope() as s:
        ok, broken = audit.verify_chain(s)
    print("OK" if ok else f"ROTA en id {broken}")
    sys.exit(0 if ok else 1)


def cmd_import_csv(args):
    from mototrade.modules.radar import CsvSource, RadarService

    with session_scope() as s:
        res = RadarService(s, get_rules()).ingest(CsvSource(open(args.path, encoding="utf-8").read(), args.source))
    print(json.dumps(res.summary(), ensure_ascii=False))


def main(argv=None):
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="mototrade")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    a = sub.add_parser("create-admin"); a.add_argument("username"); a.add_argument("password"); a.set_defaults(fn=cmd_create_admin)
    b = sub.add_parser("bootstrap"); b.add_argument("--no-capital", action="store_true"); b.set_defaults(fn=cmd_bootstrap)
    rp = sub.add_parser("reset-password"); rp.add_argument("username"); rp.set_defaults(fn=cmd_reset_password)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("db-status").set_defaults(fn=cmd_db_status)
    sub.add_parser("db-check").set_defaults(fn=cmd_db_check)
    w = sub.add_parser("worker"); w.add_argument("--once", action="store_true"); w.add_argument("--interval", type=int); w.set_defaults(fn=cmd_worker)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("verify-audit").set_defaults(fn=cmd_verify_audit)
    i = sub.add_parser("import-csv"); i.add_argument("path"); i.add_argument("--source", default="csv_import"); i.set_defaults(fn=cmd_import_csv)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
