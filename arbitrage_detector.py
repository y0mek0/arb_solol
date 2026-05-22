"""
arbitrage_detector.py — математика арбитражного расчёта.

Входные данные: две цены (Backpack и любой DEX) в USDC за 1 единицу базового токена.
Выходные данные: ArbitrageSignal с gross/net профитом и флагом is_profitable.

Направления:
  BUY_RAYDIUM_SELL_BACKPACK  — купить на DEX (дешевле), продать на Backpack (дороже)
  BUY_BACKPACK_SELL_RAYDIUM  — купить на Backpack (дешевле), продать на DEX (дороже)

Фильтры внутри detect_arbitrage():
  1. MAX_SPREAD_PCT — если gross_spread > порога → стейл/ошибка данных, is_profitable=False
  2. MIN_PROFIT_PCT — если net_profit < порога → не сигнализировать

Формула:
  gross_spread% = (sell - buy) / buy × 100
  total_costs   = raydium_fee + backpack_taker + network_fee + slippage_est
  net_profit    = trade_amount × gross_spread% / 100 − total_costs

Вызывается из: market_data.py (build_snapshot и _scan_one_pair).
Связь с CONTEXT.md: раздел «Математика арбитража» и «Фильтры качества».
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import config
from logger import log
from price_monitor import Price


class Direction(Enum):
    BUY_RAYDIUM_SELL_BACKPACK = auto()
    BUY_BACKPACK_SELL_RAYDIUM = auto()


@dataclass
class ArbitrageSignal:
    direction: Direction
    buy_price:  Price
    sell_price: Price
    gross_spread_pct: float
    net_profit_usd:   float
    net_profit_pct:   float
    trade_amount_usd: float
    is_profitable:    bool

    def __str__(self) -> str:
        arrow = "↑" if self.is_profitable else "↓"
        dir_str = (
            "Raydium→Backpack"
            if self.direction == Direction.BUY_RAYDIUM_SELL_BACKPACK
            else "Backpack→Raydium"
        )
        return (
            f"{arrow} [{dir_str}]  "
            f"spread={self.gross_spread_pct:+.3f}%  "
            f"net={self.net_profit_usd:+.4f}$  "
            f"({self.net_profit_pct:+.3f}%)"
        )


def detect_arbitrage(
    backpack_price: Optional[Price],
    raydium_price: Optional[Price],
    trade_amount_usd: float,
    sol_price_usd: float = 150.0,
) -> Optional[ArbitrageSignal]:
    if backpack_price is None or raydium_price is None:
        log.debug("detect_arbitrage: нет одной из цен, пропускаем")
        return None

    bp_p = backpack_price.price   # USDC за 1 base
    rd_p = raydium_price.price

    if bp_p == 0 or rd_p == 0:
        log.debug("detect_arbitrage: нулевая цена, пропускаем")
        return None

    # Дешевле там, где USDC за 1 base меньше
    if rd_p < bp_p:
        direction   = Direction.BUY_RAYDIUM_SELL_BACKPACK
        buy_price   = raydium_price
        sell_price  = backpack_price
        buy_p, sell_p = rd_p, bp_p
    else:
        direction   = Direction.BUY_BACKPACK_SELL_RAYDIUM
        buy_price   = backpack_price
        sell_price  = raydium_price
        buy_p, sell_p = bp_p, rd_p

    gross_spread_pct = (sell_p - buy_p) / buy_p * 100

    # Санитарный потолок: спред выше MAX_SPREAD_PCT — признак устаревшего/нерабочего рынка
    # (стейл-ордера на Backpack, разные токены, ошибка API цены Raydium и т.п.)
    if gross_spread_pct > config.MAX_SPREAD_PCT:
        signal = ArbitrageSignal(
            direction=direction,
            buy_price=buy_price,
            sell_price=sell_price,
            gross_spread_pct=gross_spread_pct,
            net_profit_usd=0.0,
            net_profit_pct=0.0,
            trade_amount_usd=trade_amount_usd,
            is_profitable=False,
        )
        log.debug(
            f"Спред {gross_spread_pct:.1f}% > MAX_SPREAD_PCT {config.MAX_SPREAD_PCT}% "
            f"— вероятно стейл/некорректные данные, пропуск"
        )
        return signal

    network_fees_usd = (
        config.ARBIT_ESTIMATED_SOL_TX_COUNT * config.NETWORK_FEE_SOL * sol_price_usd
    )
    raydium_fee_usd = trade_amount_usd * config.RAYDIUM_FEE_PCT
    backpack_fee_usd = trade_amount_usd * config.BACKPACK_TAKER_FEE_PCT
    slippage_est_usd = trade_amount_usd * (config.SLIPPAGE_BPS / 10_000) * 0.5

    total_costs_usd = (
        network_fees_usd + raydium_fee_usd + backpack_fee_usd + slippage_est_usd
    )

    gross_profit_usd = trade_amount_usd * (gross_spread_pct / 100)
    net_profit_usd   = gross_profit_usd - total_costs_usd
    net_profit_pct   = net_profit_usd / trade_amount_usd * 100

    is_profitable = net_profit_pct >= config.MIN_PROFIT_PCT

    signal = ArbitrageSignal(
        direction=direction,
        buy_price=buy_price,
        sell_price=sell_price,
        gross_spread_pct=gross_spread_pct,
        net_profit_usd=net_profit_usd,
        net_profit_pct=net_profit_pct,
        trade_amount_usd=trade_amount_usd,
        is_profitable=is_profitable,
    )

    log.debug(
        f"Арбитраж: {signal}  "
        f"(net={network_fees_usd:.4f}$ raydium_fee={raydium_fee_usd:.4f}$ "
        f"bp_fee={backpack_fee_usd:.4f}$ slip={slippage_est_usd:.4f}$)"
    )

    return signal
