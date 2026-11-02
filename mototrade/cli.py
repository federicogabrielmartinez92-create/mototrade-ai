"""CLI de operación: `python -m mototrade.cli <comando>` o `mototrade <comando>` si está instalado."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from mototrade.config import get_rules, get_settings
from mototrade.db import init_db, session_scope


def cmd_init_db(_):
    init_db()
    print("Tablas creadas/verificadas en", get_settings().database_url.split("@")[-1])


def cmd_create_admin(args):
    from mototrade.security import create_user

    with session_scope() as s:
        u = create_user(s, args.username, args.password, "admin", created_by="cli")
        print(f"Usuario admin '{u.username}' creado.")


def cmd_bootstrap(_):
    """Crea tablas, admin inicial (desde .env) y capital inicial (desde reglas)."""
    from mototrade.modules.finance import FinanceService
    from mototrade.security import ensure_admin_from_settings

    init_db()
    with session_scope() as s:
        u = ensure_admin_from_settings(s)
        cap = FinanceService(s, get_rules()).ensure_initial_capital()
    print("Admin:", "creado" if u else "ya existía / sin password en .env")
    print("Capital inicial:", "registrado" if cap else "ya existía")


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
    print("Entorno:", s.env)
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
    sub.add_parser("bootstrap").set_defaults(fn=cmd_bootstrap)
    w = sub.add_parser("worker"); w.add_argument("--once", action="store_true"); w.add_argument("--interval", type=int); w.set_defaults(fn=cmd_worker)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("verify-audit").set_defaults(fn=cmd_verify_audit)
    i = sub.add_parser("import-csv"); i.add_argument("path"); i.add_argument("--source", default="csv_import"); i.set_defaults(fn=cmd_import_csv)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
