"""
raydium_api.py — прямой доступ к Raydium v3 REST API (api-v3.raydium.io).

Это ОСНОВНОЙ источник DEX-цен (приоритет выше DexScreener для Raydium).
Jupiter Quote API был первичным источником, но часто недоступен через прокси — заменён здесь.

Публичные функции:
  get_raydium_price(base_mint, quote_mint) → float | None
      Ищет активный пул для пары, возвращает price = base/quote (USDC за 1 токен).
      Порядок поиска пулов: poolType='all' → 'Standard' → 'Concentrated'.

  get_sol_price_from_raydium() → float | None
      Цена SOL в USDC (нужна для расчёта network_fee в arbitrage_detector).

Все запросы через httpx + прокси из config.httpx_client_kwargs().
Каждый запрос записывается в proxy_monitor.monitor (latency, success/fail).

Связь с CONTEXT.md: разделы «Сетевые зависимости» и «Архитектура и поток данных».
"""

from __future__ import annotations

from typing import Optional

import httpx

import config
from price_monitor import Price
from proxy_monitor import monitor as _prx

RAYDIUM_V3_BASE = "https://api-v3.raydium.io"


def _kw(timeout: float = 20.0) -> dict:
    return config.httpx_client_kwargs(timeout=timeout)


_POOL_TYPES = ("all", "Standard", "Concentrated")


def _fetch_pools(base_mint: str, quote_mint: str, pool_type: str) -> tuple[list, Optional[str]]:
    import time as _t
    url = f"{RAYDIUM_V3_BASE}/pools/info/mint"
    params = {
        "mint1": base_mint, "mint2": quote_mint, "poolType": pool_type,
        "poolSortField": "liquidity", "sortType": "desc", "pageSize": "5", "page": "1",
    }
    t0 = _t.time()
    try:
        with httpx.Client(**_kw()) as c:
            r = c.get(url, params=params)
            lat = (_t.time() - t0) * 1000
            if r.status_code != 200:
                _prx.record(success=False, source="Raydium pools", latency_ms=lat,
                            error=f"HTTP {r.status_code}")
                return [], f"HTTP {r.status_code}"
            _prx.record(success=True, source="Raydium pools", latency_ms=lat)
            pools = r.json().get("data", {}).get("data", [])
            return pools, None
    except Exception as e:
        _prx.record(success=False, source="Raydium pools",
                    latency_ms=(_t.time()-t0)*1000, error=str(e)[:80])
        return [], str(e)


def get_raydium_price(
    base_mint: str,
    quote_mint: str,
    *,
    pool_type: str = "all",
) -> tuple[Optional[Price], Optional[str]]:
    """
    Берёт самый ликвидный пул base/quote на Raydium и возвращает цену.
    Автоматически перебирает Standard → Concentrated → AllAmm.
    """
    errors: list[str] = []
    types_to_try = [pool_type] + [t for t in _POOL_TYPES if t != pool_type]

    pools: list = []
    for pt in types_to_try:
        pl, err = _fetch_pools(base_mint, quote_mint, pt)
        if pl:
            pools = pl
            break
        errors.append(f"{pt}: {err}")

    if not pools:
        return None, "Raydium v3: " + "; ".join(errors)

    # Берём пул с наибольшей ликвидностью
    best = max(pools, key=lambda p: float(p.get("tvl") or 0))
    raw_price = best.get("price")
    pool_id = best.get("id", "?")
    ptype = best.get("type", "?")

    if raw_price is None:
        return None, "Raydium: нет поля price в пуле"

    # price в Raydium = mintA / mintB; нам нужно USDC за 1 base
    # Проверяем ориентацию: mintA и mintB в ответе
    mint_a = best.get("mintA", {}).get("address", "")
    mint_b = best.get("mintB", {}).get("address", "")

    p = float(raw_price)
    if mint_a.lower() == base_mint.lower():
        # price = base per quote → нам нужно quote per base → оставляем как есть
        usdc_per_base = p
    else:
        # price перевёрнут
        if p == 0:
            return None, "Raydium: нулевая цена"
        usdc_per_base = 1.0 / p

    return Price(
        source="raydium",
        input_mint=base_mint,
        output_mint=quote_mint,
        input_amount=1.0,
        output_amount=usdc_per_base,
        price=usdc_per_base,
        route_label=f"Raydium v3 {ptype} pool {pool_id[:12]}... tvl~{best.get('tvl',0):.0f}",
        raw_quote={"pool": best, "_source": "raydium_v3"},
    ), None


def get_sol_price_from_raydium() -> tuple[float, Optional[str]]:
    """Цена SOL через Raydium mint/price endpoint."""
    url = f"{RAYDIUM_V3_BASE}/mint/price"
    params = {"mints": config.SOL_MINT}
    try:
        with httpx.Client(**_kw()) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            p = data.get("data", {}).get(config.SOL_MINT)
            if p:
                return float(p), None
    except Exception as e:
        return 150.0, f"Raydium mint/price error: {e}"
    return 150.0, "Raydium: нет цены SOL"
