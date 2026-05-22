"""
proxy_monitor.py — мониторинг стабильности прокси и HTTP-соединений.

Singleton `monitor` импортируется во всех модулях, делающих HTTP-запросы:
    from proxy_monitor import monitor
    monitor.record(success=True, source="Backpack ticker SOL_USDC", latency_ms=210)

Что отслеживает:
  - total_ok / total_fail — общая статистика за сессию
  - consecutive_fail — текущая серия неудач (OFFLINE-индикатор)
  - reconnect_count — сколько раз соединение восстанавливалось после серии ошибок
  - last_ok_ts / last_fail_ts — временны́е метки
  - _events (deque, MAX_EVENTS=150) — лог последних событий с latency_ms

format_panel(max_log_lines) — форматирует блок для отображения в gui_app.py
    (обновляется каждые 5 сек в txt_proxy правой панели).

Источники событий (кто вызывает record()):
  - market_data.py: fetch_backpack_ticker_sync, fetch_backpack_depth_sync,
                    fetch_sol_price_usd_sync, _timed_get()
  - raydium_api.py: _fetch_pools()
  - dex_prices.py:  fetch_token_dex_prices()

Связь с CONTEXT.md: раздел «GUI — структура окна» (блок Прокси).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ProxyEvent:
    ts: float           # unix timestamp
    success: bool
    source: str         # "Backpack ticker", "Raydium v3" и т.д.
    latency_ms: float   # время запроса в мс
    error: str = ""     # краткое сообщение об ошибке


class ProxyMonitor:
    MAX_EVENTS = 150

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[ProxyEvent] = deque(maxlen=self.MAX_EVENTS)

        self.total_ok:         int   = 0
        self.total_fail:       int   = 0
        self.consecutive_fail: int   = 0
        self.reconnect_count:  int   = 0   # раз восстановились после серии ошибок
        self.last_ok_ts:       float = 0.0
        self.last_fail_ts:     float = 0.0
        self.last_fail_msg:    str   = ""

    # ── запись события ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        success: bool,
        source: str,
        latency_ms: float = 0.0,
        error: str = "",
    ) -> None:
        with self._lock:
            was_failing = self.consecutive_fail > 0
            ev = ProxyEvent(
                ts=time.time(),
                success=success,
                source=source,
                latency_ms=latency_ms,
                error=error,
            )
            self._events.append(ev)
            if success:
                self.total_ok += 1
                self.last_ok_ts = ev.ts
                if was_failing:
                    self.reconnect_count += 1
                self.consecutive_fail = 0
            else:
                self.total_fail += 1
                self.consecutive_fail += 1
                self.last_fail_ts = ev.ts
                self.last_fail_msg = error[:120] if error else "unknown"

    # ── статистика ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            total = self.total_ok + self.total_fail
            uptime = (self.total_ok / total * 100) if total else 100.0
            recent: list[ProxyEvent] = list(self._events)
            return {
                "total_ok":         self.total_ok,
                "total_fail":       self.total_fail,
                "total":            total,
                "uptime_pct":       uptime,
                "consecutive_fail": self.consecutive_fail,
                "reconnect_count":  self.reconnect_count,
                "last_ok_ts":       self.last_ok_ts,
                "last_fail_ts":     self.last_fail_ts,
                "last_fail_msg":    self.last_fail_msg,
                "recent":           recent[-30:],   # последние 30 для отображения
            }

    def format_panel(self, max_log_lines: int = 15) -> str:
        """Форматирует блок для отображения в GUI."""
        s = self.stats()
        now = time.time()

        def ago(ts: float) -> str:
            if ts == 0:
                return "—"
            d = now - ts
            if d < 60:
                return f"{d:.0f}с назад"
            if d < 3600:
                return f"{d/60:.0f}м назад"
            return time.strftime("%H:%M:%S", time.localtime(ts))

        status_line = (
            "ONLINE" if s["consecutive_fail"] == 0
            else f"OFFLINE ({s['consecutive_fail']} подряд)"
        )
        lines = [
            f"Статус:       {status_line}",
            f"Запросов:     {s['total_ok']} OK  |  {s['total_fail']} ошибок"
            f"  ({s['uptime_pct']:.1f}% uptime)",
            f"Переподкл.:   {s['reconnect_count']}x",
            f"Послед. OK:   {ago(s['last_ok_ts'])}",
            f"Послед. ERR:  {ago(s['last_fail_ts'])}",
        ]
        if s["last_fail_msg"]:
            lines.append(f"Ошибка:  {s['last_fail_msg'][:40]}")

        lines.append("-" * 34)
        lines.append("Лог (новые сверху):")

        for ev in reversed(s["recent"][-max_log_lines:]):
            mark = "OK " if ev.success else "ERR"
            t = time.strftime("%H:%M:%S", time.localtime(ev.ts))
            lat = f"{ev.latency_ms:.0f}ms" if ev.latency_ms else ""
            err = f"  {ev.error[:28]}" if not ev.success and ev.error else ""
            lines.append(f"  {mark} {t}  {ev.source[:18]:18} {lat}{err}")

        return "\n".join(lines)


# Глобальный синглтон
monitor = ProxyMonitor()
