"""
dex_prices.py — многодексовые цены через DexScreener public API.

Поддерживаемые DEX (dexId DexScreener → имя):
  raydium  → "Raydium"   (дублирует v3 API для контроля)
  orca     → "Orca"      (Whirlpools, крупнейший AMM после Raydium)
  meteora  → "Meteora"   (DLMM, динамические комиссии)
  phoenix  → "Phoenix"   (on-chain ордербук, цены ближе к CEX)

Принцип работы:
  1. fetch_token_dex_prices(base_mint) → GET DexScreener /latest/dex/tokens/{mint}
  2. Парсит все пары токена, фильтрует по quoteToken=USDC и chainId=solana
  3. Для каждого DEX берёт пул с наибольшей ликвидностью (USD)
  4. Результат кэшируется на CACHE_TTL_SEC (30с) — DexScreener обновляется ~раз в 15-30с

Важно: Raydium v3 API точнее DexScreener для Raydium-цен.
market_data.py переопределяет all_dex["Raydium"] точным v3-значением после вызова этого модуля.

best_sell_price() / best_buy_price() — утилиты для выбора лучшего DEX.

Все ошибки пишутся в proxy_monitor (singleton из proxy_monitor.py).
Связь с CONTEXT.md: раздел «Сетевые зависимости» и «Файлы — что за что отвечает».
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import httpx

import config
from proxy_monitor import monitor as _prx


# DEX ID → человеческое название (dexId из DexScreener)
TRACKED_DEXES: dict[str, str] = {
    "raydium":  "Raydium",
    "orca":     "Orca",
    "meteora":  "Meteora",
    "phoenix":  "Phoenix",
}

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
CACHE_TTL_SEC   = 30.0          # DexScreener обновляется раз в ~15-30с; 30с кэш = 1 req/30s/пара


class _PriceCache:
    """Потокобезопасный TTL-кэш: {base_mint: (timestamp, {dex_name: price})}."""

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._store: dict[str, tuple[float, dict[str, float]]] = {}

    def get_cached(self, base_mint: str) -> Optional[dict[str, float]]:
        with self._lock:
            entry = self._store.get(base_mint)
            if entry and (time.time() - entry[0]) < CACHE_TTL_SEC:
                return entry[1]
        return None

    def set(self, base_mint: str, prices: dict[str, float]) -> None:
        with self._lock:
            self._store[base_mint] = (time.time(), prices)


_cache = _PriceCache()


def _httpx_kw() -> dict:
    return config.httpx_client_kwargs(timeout=20)


def fetch_token_dex_prices(
    base_mint: str,
    quote_mint: str = "",
) -> dict[str, float]:
    """
    Возвращает {dex_name: price_usdc_per_base} для всех отслеживаемых DEX.
    quote_mint по умолчанию = USDC_MINT из config.
    Данные кэшируются на CACHE_TTL_SEC секунд.
    """
    qm = quote_mint or config.USDC_MINT

    cached = _cache.get_cached(base_mint)
    if cached is not None:
        return cached

    t0 = time.time()
    prices:  dict[str, float] = {}
    liq_map: dict[str, float] = {}

    try:
        url = DEXSCREENER_URL.format(base_mint)
        with httpx.Client(**_httpx_kw()) as c:
            r = c.get(url)
            lat = (time.time() - t0) * 1000
            r.raise_for_status()
            _prx.record(success=True, source=f"DexScr {base_mint[:8]}", latency_ms=lat)

            for pair in r.json().get("pairs") or []:
                if pair.get("chainId") != "solana":
                    continue
                dex_id = pair.get("dexId", "").lower()
                if dex_id not in TRACKED_DEXES:
                    continue

                bt = pair.get("baseToken",  {}).get("address", "")
                qt = pair.get("quoteToken", {}).get("address", "")
                price_str = pair.get("priceNative")
                if not price_str:
                    continue

                try:
                    p_native = float(price_str)
                except (ValueError, TypeError):
                    continue

                # priceNative = цена baseToken в единицах quoteToken
                if bt == base_mint and qt == qm:
                    price_usdc = p_native
                elif bt == qm and qt == base_mint:
                    price_usdc = (1.0 / p_native) if p_native else 0.0
                else:
                    continue

                if price_usdc <= 0:
                    continue

                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                dex_name = TRACKED_DEXES[dex_id]

                # Для каждого DEX берём пул с наибольшей ликвидностью
                if dex_name not in liq_map or liq > liq_map[dex_name]:
                    prices[dex_name]  = price_usdc
                    liq_map[dex_name] = liq

    except Exception as e:
        _prx.record(
            success=False, source=f"DexScr {base_mint[:8]}",
            latency_ms=(time.time() - t0) * 1000,
            error=f"{type(e).__name__}: {e!s}"[:80],
        )

    _cache.set(base_mint, prices)
    return prices


def best_sell_price(
    base_mint: str,
    raydium_price: Optional[float] = None,
    quote_mint: str = "",
) -> tuple[Optional[float], str, dict[str, float]]:
    """
    Возвращает (best_price, dex_name, all_prices) для продажи базового токена.
    best_price = максимальная цена среди всех DEX (наиболее выгодно продать).
    Raydium v3 API имеет приоритет над DexScreener для пары Raydium — он точнее.
    """
    all_prices = fetch_token_dex_prices(base_mint, quote_mint)

    # Вставляем цену Raydium v3 (если есть) — она точнее чем DexScreener для Raydium
    if raydium_price and raydium_price > 0:
        all_prices["Raydium"] = raydium_price

    if not all_prices:
        return None, "", {}

    best_name = max(all_prices, key=lambda k: all_prices[k])
    return all_prices[best_name], best_name, all_prices


def best_buy_price(
    base_mint: str,
    raydium_price: Optional[float] = None,
    quote_mint: str = "",
) -> tuple[Optional[float], str, dict[str, float]]:
    """
    Возвращает (best_price, dex_name, all_prices) для покупки базового токена.
    best_price = минимальная цена среди всех DEX (наиболее выгодно купить).
    """
    all_prices = fetch_token_dex_prices(base_mint, quote_mint)

    if raydium_price and raydium_price > 0:
        all_prices["Raydium"] = raydium_price

    if not all_prices:
        return None, "", {}

    best_name = min(all_prices, key=lambda k: all_prices[k])
    return all_prices[best_name], best_name, all_prices
