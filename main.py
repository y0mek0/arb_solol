"""
main.py — CLI-режим арбитражного монитора (УСТАРЕЛ, используй gui_app.py).

Оставлен для отладки отдельных компонентов без запуска GUI.
Основная функциональность перенесена в gui_app.py.

Режимы запуска:
  python main.py             — мониторинг + авто-своп (если ENABLE_CHAIN_EXECUTE=true)
  python main.py --monitor   — только цены в лог, без торговли
  python main.py --dry-run   — сигналы в лог, без реальных транзакций

Порядок выбора пары:
  1. Если BP_TOKEN_MINT задан и валиден → BP/USDC
  2. Иначе → SOL/USDC (демо-режим)

Ограничения CLI vs GUI:
  - Только одна пара за раз
  - Нет истории сигналов
  - Нет отображения стакана
  - DEX-источник: только Raydium через Jupiter Quote API (не v3)

Связь с CONTEXT.md: раздел «Запуск».
"""

import asyncio
import sys
import time
from datetime import datetime

import config
from arbitrage_detector import ArbitrageSignal, Direction, detect_arbitrage
from executor import execute_swap
from logger import log
from price_monitor import RaydiumLegMode, get_backpack_vs_raydium
from wallet import get_sol_balance, get_sol_price_usd, load_keypair


MONITOR_ONLY = "--monitor" in sys.argv
if "--dry-run" in sys.argv:
    config.DRY_RUN = True


class Stats:
    def __init__(self):
        self.checks = 0
        self.signals = 0
        self.trades_ok = 0
        self.trades_fail = 0
        self.total_profit_usd = 0.0
        self.start_time = time.time()

    def report(self) -> str:
        elapsed = time.time() - self.start_time
        h = int(elapsed) // 3600
        m = (int(elapsed) % 3600) // 60
        return (
            f"Время: {h}h{m:02d}m | "
            f"Проверок: {self.checks} | "
            f"Сигналов: {self.signals} | "
            f"Сделок: {self.trades_ok}✓ {self.trades_fail}✗ | "
            f"Профит: ${self.total_profit_usd:+.4f}"
        )


stats = Stats()


async def check_prerequisites(keypair) -> bool:
    if not config.BP_TOKEN_MINT or config.BP_TOKEN_MINT == "FILL_AFTER_TGE":
        log.warning(
            "BP_TOKEN_MINT не задан — режим демо **SOL/USDC** "
            "(Backpack vs Raydium). После TGE укажи mint $BP в .env."
        )
        return False
    return True


async def one_iteration(
    *,
    backpack_symbol: str,
    base_mint: str,
    quote_mint: str,
    raydium_input_mint: str,
    raydium_output_mint: str,
    raydium_amount_raw: int,
    raydium_in_decimals: int,
    raydium_out_decimals: int,
    sol_price: float,
    keypair,
) -> None:
    stats.checks += 1

    bp_price, rd_price = await get_backpack_vs_raydium(
        backpack_symbol=backpack_symbol,
        base_mint=base_mint,
        quote_mint=quote_mint,
        raydium_input_mint=raydium_input_mint,
        raydium_output_mint=raydium_output_mint,
        raydium_amount_raw=raydium_amount_raw,
        raydium_in_decimals=raydium_in_decimals,
        raydium_out_decimals=raydium_out_decimals,
        raydium_mode=RaydiumLegMode.QUOTE_IN_BASE_OUT,
    )

    ts = datetime.now().strftime("%H:%M:%S")
    bp_s = f"{bp_price.price:.4f} USDC/{bp_price.input_mint[:4]}…" if bp_price else "N/A"
    rd_s = f"{rd_price.price:.4f} USDC/{rd_price.input_mint[:4]}…" if rd_price else "N/A"
    log.info(f"[{ts}]  Backpack={bp_s}  Raydium={rd_s}")

    if MONITOR_ONLY:
        return

    signal: ArbitrageSignal | None = detect_arbitrage(
        bp_price,
        rd_price,
        trade_amount_usd=config.TRADE_AMOUNT_USDC,
        sol_price_usd=sol_price,
    )
    if signal is None:
        return

    log.info(f"Сигнал: {signal}")
    if not signal.is_profitable:
        return

    stats.signals += 1
    log.info(
        f"*** ПРОФИТНЫЙ СИГНАЛ ***  "
        f"net={signal.net_profit_usd:+.4f}$  ({signal.net_profit_pct:+.3f}%)"
    )

    # Исполнение: Backpack требует подписанный API; продажа на Raydium — другой quote (base→USDC).
    if not config.ENABLE_CHAIN_EXECUTE:
        log.info(
            "Авто-своп выключен (ENABLE_CHAIN_EXECUTE=false). "
            "Это нормально для этапа мониторинга Backpack↔Raydium."
        )
        return

    if keypair is None:
        log.warning("Keypair не загружен — пропуск исполнения")
        return

    # Единственный безопасный частичный сценарий без Backpack API: купить base на Raydium (USDC→base).
    if (
        signal.direction == Direction.BUY_RAYDIUM_SELL_BACKPACK
        and signal.buy_price.source == "raydium"
    ):
        if config.DRY_RUN:
            log.info("[DRY_RUN] Пропуск реального свопа Raydium (купить base)")
            return
        ok = await execute_swap(signal.buy_price, keypair, label="BUY_RAYDIUM")
        if ok:
            stats.trades_ok += 1
            stats.total_profit_usd += signal.net_profit_usd
            log.warning(
                "Куплено на Raydium; продажа на Backpack не автоматизирована — "
                "закрой позицию вручную на CEX или включи будущий модуль Backpack API."
            )
        else:
            stats.trades_fail += 1
        return

    log.warning(
        "Авто-исполнение для этого направления не поддерживается "
        "(нужен Backpack API и/или отдельный quote base→USDC на Raydium)."
    )


async def run_demo_sol_usdc(keypair) -> None:
    log.info("=" * 60)
    log.info("ДЕМО: Backpack vs Raydium, пара **SOL/USDC**")
    log.info(f"Backpack symbol: {config.BACKPACK_SYMBOL_SOL_USDC}")
    log.info(f"Размер quote-котировки Raydium: ~${config.TRADE_AMOUNT_USDC} USDC")
    log.info(f"Мин. профит: {config.MIN_PROFIT_PCT}% | DRY_RUN: {config.DRY_RUN}")
    log.info("=" * 60)

    amount_usdc_raw = int(config.TRADE_AMOUNT_USDC * 1e6)

    while True:
        try:
            sol_price = await get_sol_price_usd()
            await one_iteration(
                backpack_symbol=config.BACKPACK_SYMBOL_SOL_USDC,
                base_mint=config.SOL_MINT,
                quote_mint=config.USDC_MINT,
                raydium_input_mint=config.USDC_MINT,
                raydium_output_mint=config.SOL_MINT,
                raydium_amount_raw=amount_usdc_raw,
                raydium_in_decimals=6,
                raydium_out_decimals=9,
                sol_price=sol_price,
                keypair=keypair,
            )
        except Exception as e:
            log.error(f"Ошибка итерации: {e}")
        await asyncio.sleep(config.POLL_INTERVAL_SEC)


async def run_bp_usdc(keypair, bp_decimals: int = 6) -> None:
    log.info("=" * 60)
    log.info(f"Backpack vs Raydium: **BP/USDC**  mint={config.BP_TOKEN_MINT[:12]}…")
    log.info(f"Backpack symbol: {config.BACKPACK_SYMBOL_BP_USDC}")
    log.info(f"Мин. профит: {config.MIN_PROFIT_PCT}% | Slippage: {config.SLIPPAGE_BPS} bps")
    log.info("=" * 60)

    amount_usdc_raw = int(config.TRADE_AMOUNT_USDC * 1e6)

    while True:
        try:
            sol_price = await get_sol_price_usd()
            await one_iteration(
                backpack_symbol=config.BACKPACK_SYMBOL_BP_USDC,
                base_mint=config.BP_TOKEN_MINT,
                quote_mint=config.USDC_MINT,
                raydium_input_mint=config.USDC_MINT,
                raydium_output_mint=config.BP_TOKEN_MINT,
                raydium_amount_raw=amount_usdc_raw,
                raydium_in_decimals=6,
                raydium_out_decimals=bp_decimals,
                sol_price=sol_price,
                keypair=keypair,
            )
        except Exception as e:
            log.error(f"Ошибка итерации: {e}")
        await asyncio.sleep(config.POLL_INTERVAL_SEC)


async def main() -> None:
    log.info("Старт: Backpack Exchange vs Raydium (Solana)")

    keypair = None
    if not MONITOR_ONLY:
        keypair = load_keypair()
        if keypair:
            pubkey = str(keypair.pubkey())
            sol_bal = await get_sol_balance(pubkey)
            log.info(f"Кошелёк: {pubkey}  |  SOL: {sol_bal:.4f}")
            if sol_bal < 0.01 and not config.DRY_RUN and config.ENABLE_CHAIN_EXECUTE:
                log.warning("Мало SOL на комиссии.")
        else:
            log.warning("Кошелёк не задан — только мониторинг")

    bp_ready = await check_prerequisites(keypair)
    if bp_ready:
        await run_bp_usdc(keypair)
    else:
        await run_demo_sol_usdc(keypair)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"\nСтоп. {stats.report()}")
