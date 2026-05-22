"""
price_monitor.py — парсеры и типы данных для цен.

УСТАРЕВШИЙ/ВСПОМОГАТЕЛЬНЫЙ модуль. Основная логика сбора данных перенесена в:
  - market_data.py (синхронный сбор для GUI)
  - raydium_api.py (Raydium v3 REST API)
  - dex_prices.py (DexScreener для Orca/Meteora/Phoenix)

Что здесь осталось:
  Price (dataclass) — единый тип данных цены:
      price: float    — сколько USDC за 1 единицу базового токена
      source: str     — 'backpack' | 'raydium' | ...
      input_mint: str — mint входного токена (для логов)

  get_backpack_vs_raydium() — async функция, используется main.py (CLI режим)
      Запрашивает Backpack тикер + Raydium через Jupiter Quote API.
      НЕ используется в GUI (market_data.py делает то же синхронно).

  RaydiumLegMode — enum направления Raydium-котировки

Связь с CONTEXT.md: раздел «Файлы — что за что отвечает».
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

import httpx

import config
from logger import log


@dataclass
class Price:
    source: str
    input_mint: str
    output_mint: str
    input_amount: float          # «1 base» после нормализации
    output_amount: float         # USDC за 1 base
    price: float                 # = output_amount / input_amount = USDC per base
    route_label: str = ""
    raw_quote: dict = field(default_factory=dict, repr=False)

    @property
    def price_str(self) -> str:
        return f"{self.price:.6f}"


class RaydiumLegMode(Enum):
    """Как интерпретировать котировку Jupiter(Raydium-only) для USDC_per_base."""
    BASE_IN_QUOTE_OUT = auto()   # SOL → USDC: USDC_per_SOL = out / in
    QUOTE_IN_BASE_OUT = auto()   # USDC → BP: USDC_per_BP = in / out


async def _fetch_backpack_ticker(client: httpx.AsyncClient, symbol: str) -> Optional[dict[str, Any]]:
    url = f"{config.BACKPACK_API_BASE.rstrip('/')}/api/v1/ticker"
    try:
        resp = await client.get(url, params={"symbol": symbol}, timeout=5.0)
        if resp.status_code == 204:
            log.warning(f"Backpack: рынок не найден (204), symbol={symbol}")
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        log.warning(f"Backpack HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        log.warning(f"Backpack ticker error: {e}")
    return None


def price_from_backpack_ticker(
    ticker: dict[str, Any],
    *,
    base_mint: str,
    quote_mint: str,
    symbol: str,
) -> Optional[Price]:
    try:
        last = float(ticker["lastPrice"])
    except (KeyError, TypeError, ValueError):
        return None
    if last <= 0:
        return None
    return Price(
        source="backpack",
        input_mint=base_mint,
        output_mint=quote_mint,
        input_amount=1.0,
        output_amount=last,
        price=last,
        route_label=f"Backpack {symbol} lastPrice",
        raw_quote={**ticker, "_venue": "backpack", "_symbol": symbol},
    )


async def _fetch_raydium_quote(
    client: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
) -> Optional[dict]:
    params: dict = {
        "inputMint":   input_mint,
        "outputMint":  output_mint,
        "amount":      str(amount_raw),
        "slippageBps": str(config.SLIPPAGE_BPS),
        "dexes":       "Raydium",
    }
    try:
        resp = await client.get(config.JUPITER_QUOTE_URL, params=params, timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        log.warning(f"Raydium(via Jupiter) HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        log.warning(f"Raydium(via Jupiter) error: {e}")
    return None


def raydium_quote_to_price(
    data: dict,
    mode: RaydiumLegMode,
    base_mint: str,
    quote_mint: str,
    in_decimals: int,
    out_decimals: int,
) -> Optional[Price]:
    """Перевод сырого ответа Jupiter в Price с price = USDC за 1 base."""
    try:
        in_raw  = int(data["inAmount"])
        out_raw = int(data["outAmount"])
        in_amt  = in_raw  / (10 ** in_decimals)
        out_amt = out_raw / (10 ** out_decimals)
    except (KeyError, ValueError, ZeroDivisionError):
        return None

    labels = []
    for step in data.get("routePlan", []):
        labels.append(step.get("swapInfo", {}).get("label", "?"))
    route = " → ".join(labels) if labels else "Raydium"

    if mode == RaydiumLegMode.BASE_IN_QUOTE_OUT:
        # base → USDC
        if in_amt <= 0:
            return None
        usdc_per_base = out_amt / in_amt
    else:
        # USDC → base
        if out_amt <= 0:
            return None
        usdc_per_base = in_amt / out_amt

    return Price(
        source="raydium",
        input_mint=base_mint,
        output_mint=quote_mint,
        input_amount=1.0,
        output_amount=usdc_per_base,
        price=usdc_per_base,
        route_label=f"Raydium-only: {route}",
        raw_quote=data,
    )


async def get_backpack_vs_raydium(
    *,
    backpack_symbol: str,
    base_mint: str,
    quote_mint: str,
    raydium_input_mint: str,
    raydium_output_mint: str,
    raydium_amount_raw: int,
    raydium_in_decimals: int,
    raydium_out_decimals: int,
    raydium_mode: RaydiumLegMode,
) -> tuple[Optional[Price], Optional[Price]]:
    """
    Параллельно: тикер Backpack + котировка только Raydium (через Jupiter dexes=).

    Возвращает (backpack_price, raydium_price) в единицах **USDC за 1 base**.
    """
    async with httpx.AsyncClient(**config.httpx_client_kwargs(timeout=20.0)) as client:
        bp_task = _fetch_backpack_ticker(client, backpack_symbol)
        rd_task = _fetch_raydium_quote(
            client, raydium_input_mint, raydium_output_mint, raydium_amount_raw
        )
        bp_tick, rd_raw = await asyncio.gather(bp_task, rd_task)

    bp_price = (
        price_from_backpack_ticker(bp_tick, base_mint=base_mint, quote_mint=quote_mint, symbol=backpack_symbol)
        if bp_tick
        else None
    )
    rd_price = (
        raydium_quote_to_price(
            rd_raw,
            raydium_mode,
            base_mint,
            quote_mint,
            raydium_in_decimals,
            raydium_out_decimals,
        )
        if rd_raw
        else None
    )
    return bp_price, rd_price


async def get_token_decimals(mint: str) -> int:
    """Decimals SPL-токена (для BP) через Jupiter Price API."""
    try:
        async with httpx.AsyncClient(**config.httpx_client_kwargs(timeout=10.0)) as client:
            resp = await client.get(
                config.JUPITER_PRICE_URL,
                params={"ids": mint},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get(mint, {}).get("vsTokenInfo", {}).get("decimals", 6)
    except Exception:
        return 6
