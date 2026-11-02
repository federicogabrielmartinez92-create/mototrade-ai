"""Bucle del worker: corre todos los jobs cada N segundos, en su propio proceso (independiente de Streamlit/API)."""
from __future__ import annotations

import logging
import signal
import time

from mototrade.config import get_settings
from mototrade.db import session_scope
from mototrade.integrations.telegram import get_notifier

from .jobs import run_all

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, interval_seconds: int | None = None):
        self.interval = interval_seconds or self._configured_interval()
        self._stop = False

    @staticmethod
    def _configured_interval() -> int:
        """Frecuencia desde el Centro de Control (search_interval_minutes); fallback a la variable de entorno."""
        try:
            from mototrade.control import ControlCenter

            with session_scope() as s:
                return ControlCenter(s).load().search.search_interval_minutes * 60
        except Exception:  # base no inicializada todavía
            return get_settings().worker_interval_seconds

    def _handle_signal(self, *_):
        log.info("Señal recibida: deteniendo worker al terminar el ciclo actual.")
        self._stop = True

    def tick(self) -> list[dict]:
        notifier = get_notifier()
        with session_scope() as session:
            runs = run_all(session, notifier)
            out = [{"job": r.job_name, "status": r.status, "stats": r.stats, "error": r.error} for r in runs]
        for r in out:
            log.info("job=%s status=%s stats=%s %s", r["job"], r["status"], r["stats"], r["error"] or "")
        return out

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        log.info("Worker iniciado (intervalo %ss)", self.interval)
        while not self._stop:
            try:
                self.tick()
            except Exception:  # pragma: no cover - defensa
                log.exception("ciclo del worker falló")
            for _ in range(self.interval):
                if self._stop:
                    break
                time.sleep(1)
        log.info("Worker detenido.")
