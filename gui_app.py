"""
gui_app.py — главный файл приложения. Точка входа: python gui_app.py

Это GUI-приложение на CustomTkinter для мониторинга арбитражных возможностей
между Backpack Exchange (CEX) и DEX-биржами Solana (Raydium/Orca/Meteora/Phoenix).

═══ СТРУКТУРА КЛАССА ArbiApp ═══

  _build_ui()              — вся вёрстка окна (пара-selector, вкладки, история, прокси)
  _build_history_panel()   — правая панель: скан, прибыльные сигналы, прокси-статус
  _build_tab_monitor()     — вкладка «Мониторинг» (цены, спред, стакан, балансы)
  _build_tab_trade()       — вкладка «Торговля» (кнопки свопов, USDC-стратегия)

  _schedule_poll()         — запускает таймер обновления активной пары (каждые N сек)
  _refresh_async()         — в ThreadPoolExecutor: build_snapshot() → _apply_snapshot()
  _apply_snapshot(snap)    — обновляет все виджеты из MarketSnapshot

  _start_background_scanner() — daemon-поток: scan_all_pairs_light() каждые 8 сек
  _on_bg_scan(signals)     — обрабатывает LightSignal[] → _redraw_history()
  _redraw_history()        — перерисовывает правую панель (скан + история)

  _schedule_proxy_refresh() — таймер обновления прокси-панели (каждые 5 сек)
  _toggle_scan_section()   — сворачивает/разворачивает блок «Последний скан»
  _toggle_stale_section()  — сворачивает/разворачивает блок «⛔ Стейл / фантомы»
  _show_math_window()      — popup «Математика» с расчётом прибыли

═══ ПОТОКИ (THREADS) ═══
  Главный поток  → только Tkinter/after() вызовы
  ThreadPoolExecutor  → build_snapshot() (на каждый refresh активной пары)
  background_scanner  → daemon thread → scan_all_pairs_light() → after(0, _on_bg_scan)
  proxy_refresh       → таймер через after(), без доп. потока

═══ DATACLASSES ═══
  HistoryEntry     — запись в истории: пара, время, профит, длительность, math-текст
  _fmt_entry(e)    — форматирует HistoryEntry в строку для CTkTextbox

═══ СВЯЗИ С ДРУГИМИ МОДУЛЯМИ ═══
  market_data.py   → build_snapshot(), scan_all_pairs_light(), MarketSnapshot, LightSignal
  proxy_monitor.py → monitor.format_panel() → txt_proxy
  config.py        → TOKEN_PAIRS, MIN_DEPTH_USD, MAX_SPREAD_PCT, POLL_INTERVAL_SEC

Полная документация архитектуры: CONTEXT.md
"""

from __future__ import annotations

import asyncio
import threading
import time as _time
import tkinter.messagebox as messagebox
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import customtkinter as ctk

import config
from executor import execute_swap
from market_data import LightSignal, MarketSnapshot, build_snapshot, scan_all_pairs_light
from proxy_monitor import monitor as _prx_monitor
from wallet import load_keypair

MAX_HISTORY = 200        # максимум записей в истории
SCAN_INTERVAL_SEC = 8    # фоновый скан всех пар каждые N секунд
DEDUP_WINDOW_SEC  = 90   # окно дедупликации: расширяем запись, не создаём новую


@dataclass
class HistoryEntry:
    """Арбитражная запись в истории с отслеживанием длительности окна."""
    pair_key: str
    base_symbol: str
    direction: str           # "Backpack->Raydium" | "Raydium->Backpack"
    first_seen: str          # HH:MM:SS первого обнаружения
    last_seen: str           # HH:MM:SS последнего обнаружения
    first_seen_ts: float     # unix-timestamp первого
    last_seen_ts: float      # unix-timestamp последнего
    scan_count: int          # сколько сканов нашли эту возможность
    bp_price: float          # последняя цена Backpack
    ray_price: float         # последняя цена Raydium
    spread_pct: float        # gross-спред (%)
    net_profit_usd: float    # чистый профит на trade_amount_usd
    net_profit_pct: float    # чистый профит (%)
    trade_amount_usd: float  # размер сделки, на который считается профит
    is_profitable: bool = True


def _fmt_entry(e: HistoryEntry) -> str:
    """Форматирует запись в истории с полной математикой."""
    dur_sec = max(0.0, e.last_seen_ts - e.first_seen_ts)
    dur_m   = int(dur_sec // 60)
    dur_s   = int(dur_sec % 60)
    dur_str = (f"{dur_m}м {dur_s}с" if dur_m > 0 else f"{dur_s}с")
    if e.scan_count > 1:
        dur_str += f" / {e.scan_count} скан."

    time_range = (
        e.first_seen if e.scan_count == 1
        else f"{e.first_seen} -> {e.last_seen}"
    )

    if "Backpack" in e.direction.split("->")[0]:
        buy_where, buy_p  = "Backpack", e.bp_price
        sell_where, sell_p = "Raydium ", e.ray_price
    else:
        buy_where, buy_p  = "Raydium ", e.ray_price
        sell_where, sell_p = "Backpack", e.bp_price

    amt = e.trade_amount_usd
    gross_usd  = amt * e.spread_pct / 100
    rd_fee_usd = amt * config.RAYDIUM_FEE_PCT
    bp_fee_usd = amt * config.BACKPACK_TAKER_FEE_PCT
    other_usd  = gross_usd - e.net_profit_usd - rd_fee_usd - bp_fee_usd

    flag = "[ PROFIT ]" if e.is_profitable else "[  ---   ]"
    sep  = "-" * 36
    sep2 = "=" * 36

    lines = [
        f"{flag} {time_range}",
        f"Длит.: {dur_str}",
        f"Пара:  {e.pair_key}",
        f"Куда:  {e.direction}",
        sep,
        f"Купить  {e.base_symbol} @ {buy_p:.6g} USDC  ({buy_where})",
        f"Продать {e.base_symbol} @ {sell_p:.6g} USDC  ({sell_where})",
        sep,
        f"Gross спред:    {e.spread_pct:+.3f}%  = +${gross_usd:.4f}",
        f"- Raydium fee:  -{config.RAYDIUM_FEE_PCT*100:.2f}%     -${rd_fee_usd:.4f}",
        f"- Backpack fee: -{config.BACKPACK_TAKER_FEE_PCT*100:.2f}%     -${bp_fee_usd:.4f}",
    ]
    if abs(other_usd) > 0.0001:
        lines.append(f"- Сеть/прочее:         ~-${abs(other_usd):.4f}")
    lines += [
        sep2,
        f"ЧИСТЫЙ:  {e.net_profit_pct:+.3f}%  = {'+' if e.net_profit_usd>=0 else ''}{e.net_profit_usd:.4f}$",
        f"         на ${amt:.0f} USDC оборота",
        sep2,
        "",
    ]
    return "\n".join(lines)


def _snapshot_params(pair_key: str, usdc_amount: float) -> dict | None:
    pair = config.TOKEN_PAIRS.get(pair_key)
    if not pair:
        return None
    base_mint = pair["base_mint"]
    if not base_mint:
        return None
    raw = int(usdc_amount * 1e6)
    return dict(
        backpack_symbol=pair["backpack_symbol"],
        base_mint=base_mint,
        quote_mint=pair["quote_mint"],
        raydium_input_mint=pair["quote_mint"],
        raydium_output_mint=base_mint,
        raydium_amount_raw=raw,
        raydium_in_decimals=pair["quote_decimals"],
        raydium_out_decimals=pair["base_decimals"],
    )


class ArbiApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Backpack <-> Raydium")
        self.geometry("1400x860")
        self.minsize(1100, 700)

        self._executor = ThreadPoolExecutor(max_workers=3)
        self._last_snap: Optional[MarketSnapshot] = None
        self._poll_after_id: Optional[str] = None

        self._history: list[HistoryEntry] = []
        self._last_scan_all: list[LightSignal] = []   # все сигналы последнего скана
        self._scan_lock = threading.Lock()
        self._scan_expanded: bool = True              # свёрнут/развёрнут блок скана
        self._stale_expanded: bool = False            # свёрнут/развёрнут блок стейл-пар

        self._usdc_var = ctk.StringVar(value=str(config.TRADE_AMOUNT_USDC))
        self._auto_var = ctk.BooleanVar(value=True)
        self._interval_var = ctk.StringVar(value=str(int(config.POLL_INTERVAL_SEC)))
        self._dry_var = ctk.BooleanVar(value=config.DRY_RUN)

        pair_keys = list(config.TOKEN_PAIRS.keys())
        default = config.DEFAULT_PAIR if config.DEFAULT_PAIR in pair_keys else pair_keys[0]
        self._pair_var = ctk.StringVar(value=default)

        self._build_ui()
        self._schedule_poll(500)
        self._start_background_scanner()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── шапка ──
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(top, text="Пара:", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")

        pair_keys = list(config.TOKEN_PAIRS.keys())
        self._pair_selector = ctk.CTkOptionMenu(
            top,
            values=pair_keys,
            variable=self._pair_var,
            command=self._on_pair_change,
            font=ctk.CTkFont(size=13),
            width=160,
        )
        self._pair_selector.pack(side="left", padx=(6, 8))

        ctk.CTkButton(
            top, text="Математика", width=110, height=30,
            fg_color="#2d6a4f", hover_color="#1b4332",
            command=self._show_math_window,
        ).pack(side="left", padx=(0, 12))

        pl = config.proxy_label_safe()
        if pl:
            ctk.CTkLabel(top, text=f"прокси: {pl}", text_color="gray",
                         font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 10))

        self.status = ctk.CTkLabel(top, text="", text_color="#f5a623",
                                   anchor="w", font=ctk.CTkFont(size=11))
        self.status.pack(side="left", fill="x", expand=True)

        # ── основная область: левая часть (табы) + правая (история) ──
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=10, pady=2)
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=0)
        main.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(main, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew")

        right = ctk.CTkFrame(main, width=310, corner_radius=10)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.grid_propagate(False)

        self.tabs = ctk.CTkTabview(left)
        self.tabs.pack(fill="both", expand=True)
        self.tabs.add("Мониторинг")
        self.tabs.add("Торговля")

        self._build_tab_monitor(self.tabs.tab("Мониторинг"))
        self._build_tab_trade(self.tabs.tab("Торговля"))
        self._build_history_panel(right)

        # ── подвал ──
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=14, pady=(4, 10))
        ctk.CTkCheckBox(foot, text="Авто", variable=self._auto_var).pack(side="left")
        ctk.CTkLabel(foot, text="сек:").pack(side="left", padx=(12, 3))
        ctk.CTkEntry(foot, width=44, textvariable=self._interval_var).pack(side="left")
        ctk.CTkButton(foot, text="Обновить сейчас", width=130,
                      command=self._refresh_now).pack(side="left", padx=10)
        ctk.CTkLabel(foot, text="Авто-торговли нет — только кнопки во вкладке «Торговля».",
                     text_color="gray", font=ctk.CTkFont(size=11)).pack(side="right")

    def _build_tab_monitor(self, parent: ctk.CTkFrame) -> None:
        # цены + спред
        prices = ctk.CTkFrame(parent)
        prices.pack(fill="x", pady=(6, 4))
        for c in range(4):
            prices.grid_columnconfigure(c, weight=1)

        self.lbl_bp = ctk.CTkLabel(prices, text="Backpack: —", font=ctk.CTkFont(size=15))
        self.lbl_bp.grid(row=0, column=0, padx=8, pady=4, sticky="w")
        self.lbl_rd = ctk.CTkLabel(prices, text="Best DEX: —", font=ctk.CTkFont(size=15))
        self.lbl_rd.grid(row=0, column=1, padx=8, pady=4, sticky="w")
        self.lbl_spread = ctk.CTkLabel(prices, text="Спред: —", font=ctk.CTkFont(size=15))
        self.lbl_spread.grid(row=0, column=2, padx=8, pady=4, sticky="w")
        self.lbl_opp = ctk.CTkLabel(prices, text="Возможность: —", font=ctk.CTkFont(size=15),
                                    wraplength=260, justify="left")
        self.lbl_opp.grid(row=0, column=3, padx=8, pady=4, sticky="w")

        # строка со всеми DEX-ценами
        self.lbl_dex_prices = ctk.CTkLabel(
            prices, text="DEX: загрузка...",
            text_color="gray", font=ctk.CTkFont(family="Consolas", size=11),
            anchor="w",
        )
        self.lbl_dex_prices.grid(row=1, column=0, columnspan=4, padx=8, pady=(0, 4), sticky="ew")

        # стаканы
        mid = ctk.CTkFrame(parent, fg_color="transparent")
        mid.pack(fill="both", expand=True, pady=4)
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_columnconfigure(1, weight=1)
        mid.grid_rowconfigure(0, weight=1)

        self.txt_bids = ctk.CTkTextbox(mid, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_bids.grid(row=0, column=0, padx=4, pady=2, sticky="nsew")
        self.txt_asks = ctk.CTkTextbox(mid, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_asks.grid(row=0, column=1, padx=4, pady=2, sticky="nsew")

        ctk.CTkLabel(parent,
                     text="Стакан Backpack (цена | объём base | USDC уровня). "
                          "Raydium — AMM, не книга заявок; impact и маршрут ниже.",
                     text_color="gray", font=ctk.CTkFont(size=11),
                     wraplength=900, justify="left").pack(anchor="w", padx=4)

        self.txt_ray = ctk.CTkTextbox(parent, font=ctk.CTkFont(family="Consolas", size=11), height=70)
        self.txt_ray.pack(fill="x", pady=4)

        # балансы
        bal_fr = ctk.CTkFrame(parent)
        bal_fr.pack(fill="x", pady=6)
        bal_fr.grid_columnconfigure(0, weight=1)
        bal_fr.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(bal_fr, text="Solana кошелёк", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8)
        ctk.CTkLabel(bal_fr, text="Backpack баланс (API)", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, sticky="w", padx=8)
        self.txt_bal_onchain = ctk.CTkTextbox(bal_fr, font=ctk.CTkFont(family="Consolas", size=12),
                                              height=100)
        self.txt_bal_onchain.grid(row=1, column=0, padx=8, pady=2, sticky="nsew")
        self.txt_bal_bp = ctk.CTkTextbox(bal_fr, font=ctk.CTkFont(family="Consolas", size=12),
                                         height=100)
        self.txt_bal_bp.grid(row=1, column=1, padx=8, pady=2, sticky="nsew")

    def _build_tab_trade(self, parent: ctk.CTkFrame) -> None:
        ctk.CTkLabel(
            parent,
            text="Никаких автоматических ордеров. Сделка отправляется только после нажатия кнопки.\n"
                 "Своп — только Raydium (Jupiter v6). Ордера на Backpack CEX здесь не выставляются.",
            text_color="#ffcc66", justify="left",
        ).pack(anchor="w", padx=8, pady=(10, 4))

        # USDC-стратегия: напоминание
        strategy_frame = ctk.CTkFrame(parent, fg_color="#1a3a2a", corner_radius=8)
        strategy_frame.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(
            strategy_frame,
            text="⚡ Стратегия: всегда закрывай круг в USDC",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#50fa7b",
        ).pack(anchor="w", padx=10, pady=(6, 2))
        ctk.CTkLabel(
            strategy_frame,
            text="1. Купи TOKEN дешевле (нога A)  →  2. Продай TOKEN дороже (нога B)\n"
                 "Обе ноги — в течение секунд. Не держи волатильный токен — риск курсового убытка!\n"
                 "Кнопка «Математика» в шапке: подробный расчёт прибыли и комиссий.",
            text_color="#a8d8b9", justify="left", font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=10, pady=(0, 8))

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8)
        ctk.CTkLabel(row, text="Сумма USDC:").pack(side="left")
        ctk.CTkEntry(row, width=90, textvariable=self._usdc_var).pack(side="left", padx=8)
        ctk.CTkCheckBox(row, text="DRY_RUN (не отправлять в сеть)", variable=self._dry_var).pack(
            side="left", padx=12)

        self.btn_swap = ctk.CTkButton(
            parent, text="Исполнить своп USDC -> BASE на Raydium",
            command=self._on_swap_click, fg_color="#2d8f47", hover_color="#257a3c", height=40,
        )
        self.btn_swap.pack(fill="x", padx=8, pady=14)

        self.txt_trade_info = ctk.CTkTextbox(parent, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_trade_info.pack(fill="both", expand=True, padx=8, pady=6)

    def _build_history_panel(self, parent: ctk.CTkFrame) -> None:
        parent.grid_rowconfigure(2, weight=1)  # история растягивается
        parent.grid_columnconfigure(0, weight=1)

        # ── строка 0: заголовок + кнопки ──
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        ctk.CTkLabel(hdr, text="История сигналов",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="Очистить", width=70, height=24,
                      fg_color="#555", hover_color="#444",
                      command=self._clear_history).pack(side="right")

        # ── строка 1: кнопки (пакуются ПЕРВЫМИ чтобы не вытеснялись) + статус ──
        scan_bar = ctk.CTkFrame(parent, fg_color="transparent")
        scan_bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 2))

        # кнопки пакуем первыми (side="right"), иначе лейбл их вытеснит
        self.btn_stale_toggle = ctk.CTkButton(
            scan_bar,
            text="▶ ⛔",
            width=44, height=20,
            fg_color="#5a2a2a", hover_color="#6a3a3a",
            font=ctk.CTkFont(size=10),
            command=self._toggle_stale_section,
        )
        self.btn_stale_toggle.pack(side="right", padx=(2, 0))

        self.btn_scan_toggle = ctk.CTkButton(
            scan_bar,
            text="▼ скан",
            width=60, height=20,
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            font=ctk.CTkFont(size=10),
            command=self._toggle_scan_section,
        )
        self.btn_scan_toggle.pack(side="right", padx=(2, 0))

        # лейбл пакуем последним — он займёт оставшееся место
        self.lbl_scan_status = ctk.CTkLabel(
            scan_bar, text=f"скан каждые {SCAN_INTERVAL_SEC}с...",
            text_color="gray", font=ctk.CTkFont(size=10), anchor="w",
        )
        self.lbl_scan_status.pack(side="left", fill="x", expand=True)

        # ── строка 2: основной текстовый блок (скан + история) ──
        self.txt_history = ctk.CTkTextbox(
            parent, font=ctk.CTkFont(family="Consolas", size=11), wrap="word",
        )
        self.txt_history.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 4))
        self.txt_history.insert("1.0", f"Сканер запускается...\nИнтервал: {SCAN_INTERVAL_SEC}с\n")
        self.txt_history.configure(state="disabled")

        self.lbl_hist_count = ctk.CTkLabel(parent, text="Сигналов: 0",
                                           text_color="gray", font=ctk.CTkFont(size=11))
        self.lbl_hist_count.grid(row=3, column=0, sticky="w", padx=8, pady=(0, 4))

        # ── блок прокси ──
        prx_hdr = ctk.CTkFrame(parent, fg_color="transparent")
        prx_hdr.grid(row=4, column=0, sticky="ew", padx=8, pady=(6, 2))
        ctk.CTkLabel(prx_hdr, text="Прокси / соединение",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

        self.txt_proxy = ctk.CTkTextbox(
            parent,
            font=ctk.CTkFont(family="Consolas", size=10),
            height=190,
            wrap="none",
        )
        self.txt_proxy.grid(row=5, column=0, sticky="ew", padx=6, pady=(0, 8))
        self.txt_proxy.insert("1.0", "Данных ещё нет — ждём первых запросов...")
        self.txt_proxy.configure(state="disabled")

        # авто-обновление блока прокси каждые 5с
        self._schedule_proxy_refresh()

    # ── Логика опроса ─────────────────────────────────────────────────────────

    def _on_pair_change(self, value: str) -> None:
        pair = config.TOKEN_PAIRS.get(value, {})
        if not pair.get("base_mint"):
            self.status.configure(
                text="BP_TOKEN_MINT не задан в .env — укажи официальный mint.",
                text_color="#ff8844",
            )
        self._clear_prices()
        self._refresh_async()

    def _clear_prices(self) -> None:
        self.lbl_bp.configure(text="Backpack: загрузка...")
        self.lbl_rd.configure(text="Best DEX: загрузка...")
        self.lbl_spread.configure(text="Спред: —")
        self.lbl_opp.configure(text="Ждём данные...", text_color="#aaa")
        self.lbl_dex_prices.configure(text="DEX: загрузка...")
        for tb in (self.txt_bids, self.txt_asks, self.txt_ray,
                   self.txt_bal_onchain, self.txt_bal_bp, self.txt_trade_info):
            tb.configure(state="normal")
            tb.delete("1.0", "end")
        self.txt_ray.insert("1.0", "загрузка...")
        self.txt_bal_onchain.insert("1.0", "загрузка...")
        self.txt_bal_bp.insert("1.0", "загрузка...")

    def _schedule_poll(self, delay_ms: int) -> None:
        if self._poll_after_id:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        self._poll_after_id = self.after(delay_ms, self._tick)

    def _tick(self) -> None:
        if self._auto_var.get():
            self._refresh_async()
        try:
            sec = max(2, int(self._interval_var.get().strip() or "3"))
        except ValueError:
            sec = 3
        self._schedule_poll(sec * 1000)

    def _refresh_now(self) -> None:
        self._refresh_async()

    def _refresh_async(self) -> None:
        # Фиксируем активную пару прямо сейчас.
        # Результат применяем только если пользователь не успел сменить пару.
        pair_key = self._pair_var.get()

        def work() -> MarketSnapshot:
            try:
                amt = float(self._usdc_var.get().strip().replace(",", "."))
            except ValueError:
                amt = config.TRADE_AMOUNT_USDC
            kw = _snapshot_params(pair_key, amt)
            if kw is None:
                snap = MarketSnapshot(backpack_symbol=pair_key, pair_key=pair_key)
                snap.backpack_err = "base_mint не задан (BP_TOKEN_MINT пуст в .env)"
                snap.opportunity_text = "Укажи BP_TOKEN_MINT в .env — официальный mint Backpack BP."
                return snap
            kp = ""
            k = load_keypair()
            if k:
                kp = str(k.pubkey())
            snap = build_snapshot(wallet_pubkey=kp, **kw)
            snap.pair_key = pair_key
            return snap

        fut = self._executor.submit(work)

        def done() -> None:
            try:
                snap = fut.result(timeout=60)
                # Отбрасываем если пользователь успел переключить пару
                if snap.pair_key != self._pair_var.get():
                    return
                self.after(0, lambda: self._apply_snapshot(snap))
            except Exception as e:
                # Фиксируем сообщение сразу — e удаляется Python после except-блока
                err_msg = str(e)
                try:
                    sec = max(2, int(self._interval_var.get().strip() or "3"))
                except ValueError:
                    sec = 3
                self.after(0, lambda msg=err_msg, s=sec: self.status.configure(
                    text=f"Ошибка: {msg[:120]}  (авто-повтор через {s}с)",
                    text_color="#ff6666",
                ))

        threading.Thread(target=done, daemon=True).start()

    # ── Применение снимка ─────────────────────────────────────────────────────

    def _apply_snapshot(self, snap: MarketSnapshot) -> None:
        self._last_snap = snap

        # статус-строка
        st = f"Обновлено {snap.time_iso}"
        if snap.backpack_err:
            st += f" | Backpack: {snap.backpack_err[:80]}"
        if snap.raydium_err:
            st += f" | Raydium: {snap.raydium_err[:100]}"
        if snap.sol_price_note:
            st += f" | {snap.sol_price_note[:60]}"
        self.status.configure(text=st, text_color="#f5a623")

        pair_key = snap.pair_key or self._pair_var.get()
        unit = config.TOKEN_PAIRS.get(pair_key, {}).get("base_symbol", "BASE")

        self.lbl_bp.configure(
            text=f"Backpack: {snap.backpack_usdc_per_base:.5g} USDC/{unit}"
            if snap.backpack_usdc_per_base else "Backpack: —"
        )
        # Best DEX label
        best_dex_p = snap.dex_prices.get(snap.best_dex_name) if snap.dex_prices else None
        best_dex_p = best_dex_p or snap.raydium_usdc_per_base
        self.lbl_rd.configure(
            text=f"{snap.best_dex_name}: {best_dex_p:.5g} USDC/{unit}"
            if best_dex_p else "Best DEX: — (нет пулов / сеть)"
        )
        # Строка всех DEX-цен
        if snap.dex_prices:
            parts = []
            for dex, p in sorted(snap.dex_prices.items()):
                marker = "★" if dex == snap.best_dex_name else " "
                parts.append(f"{marker}{dex}={p:.5g}")
            self.lbl_dex_prices.configure(text="DEX: " + "  |  ".join(parts))
        else:
            self.lbl_dex_prices.configure(text="DEX: загрузка цен...")
        if snap.spread_pct is not None:
            eff_parts = []
            if snap.eff_bp_bid_price:
                eff_parts.append(f"bid₃₀={snap.eff_bp_bid_price:.5g}")
            if snap.eff_bp_ask_price:
                eff_parts.append(f"ask₃₀={snap.eff_bp_ask_price:.5g}")
            eff_str = "  " + "  ".join(eff_parts) if eff_parts else f"  ⚠ нет ур. >=${config.MIN_DEPTH_USD:.0f}"
            self.lbl_spread.configure(text=f"Спред: {snap.spread_pct:.3f}%{eff_str}")
        else:
            self.lbl_spread.configure(text="Спред: —")

        # Определяем причину блокировки для цвета и заголовка
        opp_text = snap.opportunity_text or ""
        is_stale = (
            snap.spread_pct is not None
            and snap.spread_pct > config.MAX_SPREAD_PCT
            and not snap.opportunity_profitable
        )
        if snap.opportunity_profitable:
            col = "#5fcf7a"
            opp_header = "*** ВЫГОДНО ***"
        elif is_stale:
            col = "#ff6666"
            opp_header = (
                f"⛔ СТЕЙЛ / ОШИБКА ДАННЫХ  (спред {snap.spread_pct:.1f}% > "
                f"MAX {config.MAX_SPREAD_PCT:.0f}%)\n"
                f"Вероятно: нерабочий рынок Backpack или ошибка цены Raydium."
            )
        else:
            col = "#aaa"
            opp_header = "Нет / ждём"

        self.lbl_opp.configure(
            text=f"{opp_header}\n{opp_text[:160]}" if not is_stale else opp_header,
            text_color=col,
        )

        # ── стаканы ───────────────────────────────────────────────────────────
        min_dep = config.MIN_DEPTH_USD

        def fill_table(tb: ctk.CTkTextbox, title: str, rows: list, cum: float,
                       eff_price: Optional[float]) -> None:
            tb.configure(state="normal")
            tb.delete("1.0", "end")
            eff_str = f"  eff={eff_price:.5g}" if eff_price else f"  нет >=${min_dep:.0f}"
            tb.insert("1.0", f"{title}  (~{cum:,.0f} USDC){eff_str}\n")
            tb.insert("end", f"{'Цена':>12} {'Base':>14} {'USDC':>12}\n")
            tb.insert("end", "-" * 40 + "\n")
            for entry in rows:
                p, q, u = entry[0], entry[1], entry[2]
                is_liquid = entry[3] if len(entry) > 3 else False
                mark = "►" if is_liquid else " "
                tb.insert("end", f"{mark}{p:>11} {q:>14} {u:>12}\n")
            tb.configure(state="disabled")

        if snap.depth_err:
            fill_table(self.txt_bids, "BIDS", [], 0, None)
            self.txt_bids.configure(state="normal")
            self.txt_bids.insert("end", f"\n{snap.depth_err}")
            self.txt_bids.configure(state="disabled")
            fill_table(self.txt_asks, "ASKS", [], 0, None)
        else:
            fill_table(self.txt_bids, "BIDS", snap.depth_bid_rows,
                       snap.depth_bid_liquidity_usdc, snap.eff_bp_bid_price)
            fill_table(self.txt_asks, "ASKS", snap.depth_ask_rows,
                       snap.depth_ask_liquidity_usdc, snap.eff_bp_ask_price)

        self.txt_ray.configure(state="normal")
        self.txt_ray.delete("1.0", "end")
        self.txt_ray.insert(
            "1.0",
            f"Raydium  impact: {snap.raydium_impact_pct or '—'}   "
            f"маршрут: {snap.raydium_route or '—'}   "
            f"(AMM — не книга заявок)\n",
        )
        self.txt_ray.configure(state="disabled")

        # балансы
        self.txt_bal_onchain.configure(state="normal")
        self.txt_bal_onchain.delete("1.0", "end")
        if snap.on_chain_err:
            self.txt_bal_onchain.insert("1.0", snap.on_chain_err)
        else:
            self.txt_bal_onchain.insert(
                "1.0",
                f"Адрес: {snap.wallet_address or '—'}\n"
                f"SOL:   {snap.on_chain.get('SOL', 0):.6f}\n"
                f"USDC:  {snap.on_chain.get('USDC', 0):.4f}\n",
            )
        self.txt_bal_onchain.configure(state="disabled")

        self.txt_bal_bp.configure(state="normal")
        self.txt_bal_bp.delete("1.0", "end")
        if snap.backpack_balance_err:
            self.txt_bal_bp.insert("1.0", snap.backpack_balance_err)
        elif snap.backpack_balances:
            lines = []
            for asset, row in snap.backpack_balances.items():
                if isinstance(row, dict):
                    av = row.get("available", "?")
                    lk = row.get("locked", "?")
                    lines.append(f"{asset:6}: {av}  (locked: {lk})")
                else:
                    lines.append(f"{asset}: {row}")
            self.txt_bal_bp.insert("1.0", "\n".join(lines) if lines else "нет данных")
        else:
            self.txt_bal_bp.insert("1.0", "Пусто или нет данных")
        self.txt_bal_bp.configure(state="disabled")

        # вкладка торговли
        self.txt_trade_info.configure(state="normal")
        self.txt_trade_info.delete("1.0", "end")
        rp = snap.raydium_price
        if rp:
            self.txt_trade_info.insert(
                "1.0",
                f"Котировка Raydium (USDC -> {unit}):\n"
                f"  in  = {rp.raw_quote.get('inAmount')} мин. ед. USDC\n"
                f"  out ~= {rp.raw_quote.get('outAmount')} мин. ед. {unit}\n"
                f"  маршрут: {rp.route_label}\n",
            )
        else:
            self.txt_trade_info.insert("1.0", "Нет котировки Raydium — кнопка свопа не сработает.\n")
        self.txt_trade_info.configure(state="disabled")

        can_swap = bool(snap.raydium_price and snap.raydium_price.raw_quote.get("routePlan"))
        self.btn_swap.configure(state="normal" if can_swap else "disabled")

        # история из детального снимка (только прибыльные)
        if snap.opportunity_profitable and snap.net_profit_usd is not None:
            self._add_history_from_snap(snap, pair_key)

    def _start_background_scanner(self) -> None:
        """Запускает фоновый поток для скана ВСЕХ пар каждые SCAN_INTERVAL_SEC секунд."""
        def scan_loop() -> None:
            _time.sleep(3)  # небольшая пауза при старте
            while True:
                try:
                    sol_p = getattr(self._last_snap, "sol_price_usd", 150.0) or 150.0
                    signals = scan_all_pairs_light(sol_price_usd=sol_p)
                    profitable = [s for s in signals if s.is_profitable]
                    pairs_ok = len(signals)
                    all_pairs = len([v for v in config.TOKEN_PAIRS.values() if v.get("base_mint")])
                    status_txt = (
                        f"{_time.strftime('%H:%M')}  "
                        f"{pairs_ok}/{all_pairs} пар  "
                        f"прибыльных: {len(profitable)}"
                    )
                    self.after(0, lambda t=status_txt: self.lbl_scan_status.configure(
                        text=t, text_color=("#5fcf7a" if profitable else "gray")))

                    # передаём ВСЕ сигналы (для статуса спредов) + прибыльные в историю
                    self.after(0, lambda all_s=signals, prof=profitable: self._on_bg_scan(all_s, prof))
                except Exception as e:
                    self.after(0, lambda err=str(e): self.lbl_scan_status.configure(
                        text=f"Скан ошибка: {err[:60]}", text_color="#ff6666"))
                _time.sleep(SCAN_INTERVAL_SEC)

        t = threading.Thread(target=scan_loop, daemon=True, name="bg-scanner")
        t.start()

    def _sig_to_entry(self, sig: LightSignal, now: float) -> HistoryEntry:
        return HistoryEntry(
            pair_key=sig.pair_key,
            base_symbol=sig.base_symbol,
            direction=sig.direction,
            first_seen=sig.time_str,
            last_seen=sig.time_str,
            first_seen_ts=now,
            last_seen_ts=now,
            scan_count=1,
            bp_price=sig.bp_price,
            ray_price=sig.ray_price,
            spread_pct=sig.spread_pct,
            net_profit_usd=sig.net_profit_usd,
            net_profit_pct=sig.net_profit_pct,
            trade_amount_usd=sig.trade_amount_usd,
            is_profitable=sig.is_profitable,
        )

    def _upsert_entry(self, new_e: HistoryEntry) -> None:
        """Добавляет запись или расширяет существующую (дедупликация по паре+направлению).
        Записи с net_profit_pct ниже MIN_PROFIT_PCT игнорируются."""
        if new_e.net_profit_pct < config.MIN_PROFIT_PCT:
            return
        now = new_e.last_seen_ts
        for e in self._history[:15]:  # проверяем только последние
            if (e.pair_key == new_e.pair_key
                    and e.direction == new_e.direction
                    and (now - e.last_seen_ts) < DEDUP_WINDOW_SEC):
                e.last_seen    = new_e.last_seen
                e.last_seen_ts = now
                e.scan_count  += 1
                e.bp_price     = new_e.bp_price
                e.ray_price    = new_e.ray_price
                e.spread_pct   = new_e.spread_pct
                e.net_profit_usd = new_e.net_profit_usd
                e.net_profit_pct = new_e.net_profit_pct
                return
        # новая запись — вставляем в начало
        self._history.insert(0, new_e)
        if len(self._history) > MAX_HISTORY:
            self._history = self._history[:MAX_HISTORY]

    def _on_bg_scan(self, all_signals: list[LightSignal], profitable: list[LightSignal]) -> None:
        """Вызывается в главном потоке после каждого скана."""
        self._last_scan_all = all_signals
        now = _time.time()
        if profitable:
            with self._scan_lock:
                for sig in profitable:
                    entry = self._sig_to_entry(sig, now)
                    self._upsert_entry(entry)
        self._redraw_history()

    def _add_history_from_snap(self, snap: MarketSnapshot, pair_key: str) -> None:
        """Добавляет запись из детального снимка (отображаемая пара)."""
        if snap.net_profit_usd is None:
            return
        try:
            direction = snap.opportunity_text.split("[")[1].split("]")[0]
        except Exception:
            direction = "Raydium->Backpack"
        pct = snap.net_profit_pct or (
            snap.net_profit_usd / config.TRADE_AMOUNT_USDC * 100
            if config.TRADE_AMOUNT_USDC else 0.0
        )
        now = _time.time()
        entry = HistoryEntry(
            pair_key=pair_key,
            base_symbol=config.TOKEN_PAIRS.get(pair_key, {}).get("base_symbol", "?"),
            direction=direction,
            first_seen=snap.time_iso,
            last_seen=snap.time_iso,
            first_seen_ts=now,
            last_seen_ts=now,
            scan_count=1,
            bp_price=snap.backpack_usdc_per_base or 0.0,
            ray_price=snap.raydium_usdc_per_base or 0.0,
            spread_pct=snap.spread_pct or 0.0,
            net_profit_usd=snap.net_profit_usd,
            net_profit_pct=pct,
            trade_amount_usd=config.TRADE_AMOUNT_USDC,
            is_profitable=snap.opportunity_profitable,
        )
        with self._scan_lock:
            self._upsert_entry(entry)
        self._redraw_history()

    def _redraw_history(self) -> None:
        self.txt_history.configure(state="normal")
        self.txt_history.delete("1.0", "end")

        # ── разделяем скан на обычные и стейл-пары ──
        normal_scans = [s for s in self._last_scan_all
                        if not (s.spread_pct and s.spread_pct > config.MAX_SPREAD_PCT)]
        stale_scans  = [s for s in self._last_scan_all
                        if s.spread_pct and s.spread_pct > config.MAX_SPREAD_PCT]

        # ── блок «Последний скан» (без стейл) ──
        if self._last_scan_all:
            n_profit  = sum(1 for s in normal_scans if s.is_profitable)
            arrow = "▼" if self._scan_expanded else "▶"
            self.txt_history.insert(
                "end",
                f"{arrow} Последний скан  "
                f"[{len(normal_scans)} пар | прибыльных: {n_profit}]\n",
            )
            if self._scan_expanded:
                self.txt_history.insert("end", "-" * 34 + "\n")
                for s in sorted(normal_scans, key=lambda x: x.net_profit_pct, reverse=True):
                    mark = ">>>" if s.is_profitable else "   "
                    bp_str = f"{s.bp_price:.5g}" if s.bp_price is not None else "—"
                    rd_str = f"{s.ray_price:.5g}" if s.ray_price is not None else "—"
                    self.txt_history.insert(
                        "end",
                        f"{mark} {s.pair_key:10}  {s.spread_pct:+.3f}%"
                        f"  BP={bp_str}  RD={rd_str}\n"
                        f"         {s.direction}  ~{s.net_profit_pct:+.2f}%\n",
                    )
                self.txt_history.insert("end", "-" * 34 + "\n")
        else:
            self.txt_history.insert(
                "end",
                f"▶ Сканер запускается...\n"
                f"  Все пары проверяются каждые {SCAN_INTERVAL_SEC}с.\n",
            )

        # ── блок «⛔ Стейл / ошибки данных» — отдельный, сворачиваемый ──
        if stale_scans:
            arrow_s = "▼" if self._stale_expanded else "▶"
            self.txt_history.insert(
                "end",
                f"{arrow_s} ⛔ Стейл / фантомы  [{len(stale_scans)} пар]\n",
            )
            if self._stale_expanded:
                self.txt_history.insert("end", "- " * 17 + "\n")
                for s in sorted(stale_scans, key=lambda x: x.spread_pct, reverse=True):
                    bp_str = f"{s.bp_price:.5g}" if s.bp_price is not None else "—"
                    rd_str = f"{s.ray_price:.5g}" if s.ray_price is not None else "—"
                    self.txt_history.insert(
                        "end",
                        f"  ⛔ {s.pair_key:10}  {s.spread_pct:+.1f}%\n"
                        f"     BP={bp_str}  RD={rd_str}\n",
                    )
                self.txt_history.insert("end", "- " * 17 + "\n")

        self.txt_history.insert("end", "\n")

        # ── прибыльные сигналы ≥ MIN_PROFIT_PCT (всегда видны) ──
        profitable_hist = [e for e in self._history if e.is_profitable
                           and e.net_profit_pct >= config.MIN_PROFIT_PCT]
        n_profit_hist = len(profitable_hist)
        if not profitable_hist:
            self.txt_history.insert(
                "end",
                f"Прибыльных сигналов ≥{config.MIN_PROFIT_PCT:.1f}% пока не было.\n",
            )
        else:
            self.txt_history.insert(
                "end",
                f"История: {n_profit_hist} сигналов ≥{config.MIN_PROFIT_PCT:.1f}%\n"
                + "=" * 36 + "\n\n",
            )
            for e in profitable_hist:
                self.txt_history.insert("end", _fmt_entry(e))

        self.txt_history.configure(state="disabled")
        self.txt_history.see("1.0")
        self.lbl_hist_count.configure(
            text=f"Прибыльных ≥{config.MIN_PROFIT_PCT:.1f}%: {n_profit_hist}"
        )

    def _toggle_scan_section(self) -> None:
        self._scan_expanded = not self._scan_expanded
        self.btn_scan_toggle.configure(
            text="▼ скан" if self._scan_expanded else "▶ скан"
        )
        self._redraw_history()

    def _toggle_stale_section(self) -> None:
        self._stale_expanded = not self._stale_expanded
        self.btn_stale_toggle.configure(
            text="▼ ⛔" if self._stale_expanded else "▶ ⛔"
        )
        self._redraw_history()

    def _clear_history(self) -> None:
        with self._scan_lock:
            self._history.clear()
        self._redraw_history()

    # ── Окно «Математика» ─────────────────────────────────────────────────────

    def _show_math_window(self) -> None:
        """Всплывающее окно с разбором математики арбитража и стратегией USDC."""
        win = ctk.CTkToplevel(self)
        win.title("Математика арбитража")
        win.geometry("680x760")
        win.resizable(True, True)
        win.lift()
        win.focus_force()

        txt = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        txt.pack(fill="both", expand=True, padx=10, pady=10)

        trade = config.TRADE_AMOUNT_USDC
        ray_fee = config.RAYDIUM_FEE_PCT * 100          # %
        bp_fee  = config.BACKPACK_TAKER_FEE_PCT * 100   # %
        net_fee = config.NETWORK_FEE_SOL
        # примерная стоимость сети при SOL=130$
        net_usd_approx = net_fee * 130

        content = f"""╔══════════════════════════════════════════════════════════════╗
║         КАК РАБОТАЕТ АРБИТРАЖ И КАК СЧИТАЕТСЯ ПРИБЫЛЬ        ║
╚══════════════════════════════════════════════════════════════╝

━━━━━━━━━━━━━━━━  ИДЕЯ  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Токен одновременно торгуется на двух площадках:
  • Backpack Exchange — централизованный CEX (ордербук)
  • Raydium DEX — автоматический маркет-мейкер на блокчейне

Из-за разного спроса/предложения цена расходится.
Арбитражёр:
  1. Покупает дешевле на одной бирже.
  2. Продаёт дороже на другой.
  3. Разница = прибыль.

━━━━━━━━━━━━━━  НАПРАВЛЕНИЯ  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Backpack → Raydium (BP дороже):
  Купить TOKEN на Backpack (дешевле)
  Продать TOKEN на Raydium (дороже)

Raydium → Backpack (Raydium дороже):
  Купить TOKEN на Raydium (дешевле)
  Продать TOKEN на Backpack (дороже)

━━━━━━━━━━━━━━  ФОРМУЛА  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  gross_spread% = |BP_price - Ray_price| / min(BP,Ray) × 100

  total_fee% = Raydium_fee + Backpack_taker + network_overhead
             = {ray_fee:.2f}%  +  {bp_fee:.2f}%  +  ~{net_usd_approx/trade*100:.2f}%
             ≈ {ray_fee + bp_fee + net_usd_approx/trade*100:.2f}% (при сделке ${trade:.0f})

  net_profit% = gross_spread% − total_fee%
  net_profit_usd = trade_usd × net_profit% / 100

  ⚡ Сигнал прибыльный только если net_profit% > MIN_PROFIT% ({config.MIN_PROFIT_PCT}%)

━━━━━━━━━━━━  ПРИМЕР: SOL/USDC (${trade:.0f})  ━━━━━━━━━━━━━━━━

  Backpack: SOL = 130.00 USDC  (продают дешевле)
  Raydium:  SOL = 130.80 USDC  (покупают дороже)

  Gross-спред = (130.80 − 130.00) / 130.00 × 100 = 0.615%

  Токенов куплено на Backpack:
    ${trade:.2f} / 130.00 = {trade/130:.5f} SOL

  Выручка от продажи на Raydium:
    {trade/130:.5f} SOL × 130.80 = ${trade/130*130.80:.4f}

  Комиссии:
    Raydium swap fee:  ${trade:.2f} × {ray_fee:.2f}% = ${trade*config.RAYDIUM_FEE_PCT:.4f}
    Backpack taker:    ${trade:.2f} × {bp_fee:.2f}%  = ${trade*config.BACKPACK_TAKER_FEE_PCT:.4f}
    Сеть Solana:       ~${net_usd_approx:.4f}

  Итого комиссии: ~${trade*config.RAYDIUM_FEE_PCT + trade*config.BACKPACK_TAKER_FEE_PCT + net_usd_approx:.4f}

  Чистый профит:
    ${trade/130*130.80 - trade - (trade*config.RAYDIUM_FEE_PCT + trade*config.BACKPACK_TAKER_FEE_PCT + net_usd_approx):.4f}
    = {(trade/130*130.80 - trade - (trade*config.RAYDIUM_FEE_PCT + trade*config.BACKPACK_TAKER_FEE_PCT + net_usd_approx))/trade*100:.3f}%

━━━━━━━━━━  МИНИМАЛЬНЫЙ ПОРОГ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Чтобы сигнал попал в историю:
    net_profit% ≥ {config.MIN_PROFIT_PCT}%  (MIN_PROFIT_PCT в .env)

  При ${trade:.0f} сделке это ≥ ${trade * config.MIN_PROFIT_PCT / 100:.4f}

  Совет: с маленьким капиталом ставь MIN_PROFIT_PCT = 0.3–0.5%
         Увеличь TRADE_AMOUNT_USDC для роста абсолютного профита.

━━━━━━━━━━━━  СТРАТЕГИЯ: ДЕРЖАТЬ В USDC  ━━━━━━━━━━━━━━━━━━

  ⚠ Не держи волатильный токен после сделки!

  Правило:
    • Каждая сделка должна ЗАВЕРШИТЬ КРУГ в USDC:
        USDC → TOKEN (покупка) → USDC (продажа)

  Пример полного круга:
    1. Купить SOL на Backpack за $10 USDC
    2. Сразу же продать SOL на Raydium за ~$10.06 USDC
    Итог: ты снова в USDC, но с профитом $0.06

  Если совершена только ОДНА нога (купил, но не продал):
    • Ты держишь TOKEN, чья цена может упасть
    • Будущий арбитраж нивелируется курсовым убытком

  Алгоритм в этой программе:
    → Кнопка «Купить» (Raydium) = ПЕРВАЯ нога
    → После этого НЕМЕДЛЕННО нажми «Продать» на другой бирже
    → Обе ноги должны выполняться в течение СЕКУНД, не минут

  В будущей авто-версии обе ноги будут атомарными (flash-arb).

━━━━━━━━━━━━━━━━  СЛИППЕДЖ  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Slippage = реальная цена исполнения хуже цены котировки.
  На Raydium AMM при $10 обычно 0.1–0.3% (зависит от ликвидности пула).
  Текущая настройка: SLIPPAGE_BPS = {config.SLIPPAGE_BPS} ({config.SLIPPAGE_BPS/100:.1f}%)

  Чем больше сделка и меньше ликвидность — тем хуже исполнение.
  Для мемкоинов (BONK, WIF) slippage может быть 0.5–1%.

━━━━━━━━━━  НАСТРОЙКИ (.env)  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  MIN_PROFIT_PCT       = {config.MIN_PROFIT_PCT}    (мин. чистый % для входа)
  TRADE_AMOUNT_USDC    = {config.TRADE_AMOUNT_USDC}   (размер позиции, $)
  MIN_DEPTH_USD        = {config.MIN_DEPTH_USD}  (мин. объём ближайшего уровня стакана)
  MAX_SPREAD_PCT       = {config.MAX_SPREAD_PCT}  (потолок спреда — выше = ошибка данных)
  SLIPPAGE_BPS         = {config.SLIPPAGE_BPS}  (допустимый slippage)
  BACKPACK_TAKER_FEE   = {bp_fee:.3f}% (уточни в настройках Backpack)
  RAYDIUM_FEE          = {ray_fee:.3f}% (фиксированная для стандарт. пулов)

━━━━━━━━━━━━━━━  ДВОЙНОЙ ФИЛЬТР КАЧЕСТВА  ━━━━━━━━━━━━━━━━━━

  1. MIN_DEPTH_USD = ${config.MIN_DEPTH_USD:.0f}
     Ищем в стакане ПЕРВЫЙ уровень с реальным объёмом.
     Все копеечные ордера ($0.01–$1) до него игнорируются.
     Именно эта цена используется для расчёта спреда.

  2. MAX_SPREAD_PCT = {config.MAX_SPREAD_PCT:.0f}%
     Даже с реальным объёмом: если спред > {config.MAX_SPREAD_PCT:.0f}% →
     это не арбитраж, а нерабочий рынок.
     Причины: стейл-ордера на Backpack, неактивная пара,
     разные версии токена, ошибка в цене Raydium.
     Реальный арбитраж = 0.3–5%, максимум 15% на мемкоинах.
"""
        txt.insert("1.0", content)
        txt.configure(state="disabled")

        ctk.CTkButton(win, text="Закрыть", command=win.destroy).pack(pady=(0, 10))

    # ── Прокси-монитор ────────────────────────────────────────────────────────

    def _schedule_proxy_refresh(self) -> None:
        self._refresh_proxy_panel()
        self.after(5_000, self._schedule_proxy_refresh)

    def _refresh_proxy_panel(self) -> None:
        text = _prx_monitor.format_panel(max_log_lines=12)
        self.txt_proxy.configure(state="normal")
        self.txt_proxy.delete("1.0", "end")
        self.txt_proxy.insert("1.0", text)
        self.txt_proxy.configure(state="disabled")

    # ── Своп ──────────────────────────────────────────────────────────────────

    def _on_swap_click(self) -> None:
        if not self._last_snap or not self._last_snap.raydium_price:
            messagebox.showwarning("Нет данных", "Сначала дождись котировки Raydium.")
            return
        if not messagebox.askyesno(
            "Подтверждение",
            "Отправить транзакцию свопа USDC -> BASE на Raydium?\n"
            "(DRY_RUN — tx не уйдёт в сеть.)",
        ):
            return

        def work() -> tuple[bool, str]:
            kp = load_keypair()
            if not kp:
                return False, "Нет WALLET_PRIVATE_KEY в .env"
            old = config.DRY_RUN
            config.DRY_RUN = bool(self._dry_var.get())
            try:
                ok = asyncio.run(
                    execute_swap(self._last_snap.raydium_price, kp, label="GUI_RAYDIUM")  # type: ignore
                )
                return ok, "Готово" if ok else "Своп не выполнен (см. arbi.log)"
            except Exception as e:
                return False, str(e)
            finally:
                config.DRY_RUN = old

        self.btn_swap.configure(state="disabled")

        def thread_target() -> None:
            ok, msg = work()

            def finish() -> None:
                self.btn_swap.configure(state="normal")
                (messagebox.showinfo if ok else messagebox.showerror)("Результат", msg)

            self.after(0, finish)

        threading.Thread(target=thread_target, daemon=True).start()


def main() -> None:
    app = ArbiApp()
    app.mainloop()


if __name__ == "__main__":
    main()
