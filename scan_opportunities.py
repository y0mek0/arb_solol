"""
scan_opportunities.py — анализатор пар для арбитража Backpack <-> Solana DEX.

Шаги:
  1. Получить все SPOT USDC-пары с Backpack Exchange
  2. Для каждой — найти токен на DexScreener (Solana)
  3. Отфильтровать по ликвидности и объёму
  4. Вывести таблицу с метриками + рекомендации для добавления в бот

Запуск: python scan_opportunities.py
"""

from __future__ import annotations
import sys, time, math
import httpx
import config

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── константы ──────────────────────────────────────────────────────────────
BACKPACK_MARKETS  = "https://api.backpack.exchange/api/v1/markets"
BACKPACK_TICKERS  = "https://api.backpack.exchange/api/v1/tickers"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={}"
DEXSCREENER_TOKEN  = "https://api.dexscreener.com/latest/dex/tokens/{}"

MIN_BP_VOL_USD    = 15_000    # мин. объём Backpack за 24ч в USDC
MIN_DEX_LIQ_USD   = 30_000    # мин. ликвидность пула на DEX в USD
MIN_DEX_VOL_USD   = 5_000     # мин. объём на DEX за 24ч в USD

TRACKED_DEXES = {"raydium", "orca", "meteora", "phoenix", "lifinity"}

ALREADY_IN_BOT = set(config.TOKEN_PAIRS.keys())

# mint из нашего config — для быстрого поиска без лишних запросов
_SYMBOL_TO_MINT: dict[str, str] = {
    v["base_symbol"]: v["base_mint"]
    for v in config.TOKEN_PAIRS.values()
    if v.get("base_mint") and v.get("base_symbol")
}

# ── helpers ────────────────────────────────────────────────────────────────

def _client() -> httpx.Client:
    return httpx.Client(**config.httpx_client_kwargs())


def fetch_backpack_markets(client: httpx.Client) -> list[dict]:
    r = client.get(BACKPACK_MARKETS)
    r.raise_for_status()
    data = r.json()
    return [
        m for m in data
        if m.get("quoteSymbol", "").upper() == "USDC"
        and m.get("marketType") == "SPOT"
        and m.get("visible", True)
    ]


def fetch_backpack_tickers(client: httpx.Client) -> dict[str, dict]:
    r = client.get(BACKPACK_TICKERS)
    r.raise_for_status()
    raw = r.json()
    return {t["symbol"]: t for t in raw} if isinstance(raw, list) else raw


def _parse_dex_pairs(pairs: list, symbol: str) -> dict:
    """Из списка DexScreener пар выбирает лучшие пулы на Solana для токена."""
    # фильтруем: только Solana, только нужные DEX, только USDC или SOL как quote
    filtered = [
        p for p in pairs
        if p.get("chainId") == "solana"
        and p.get("dexId", "").lower() in TRACKED_DEXES
        and (
            p.get("baseToken", {}).get("symbol", "").upper() == symbol.upper()
            or p.get("quoteToken", {}).get("symbol", "").upper() == symbol.upper()
        )
        and p.get("quoteToken", {}).get("symbol", "").upper() in ("USDC", "SOL")
    ]
    if not filtered:
        return {}

    total_liq = sum(float(p.get("liquidity", {}).get("usd", 0) or 0) for p in filtered)
    total_vol = sum(float(p.get("volume", {}).get("h24", 0) or 0) for p in filtered)

    best = max(filtered, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    price_usd = float(best.get("priceUsd", 0) or 0)

    # если токен стоит как quoteToken — инвертируем цену
    if best.get("quoteToken", {}).get("symbol", "").upper() == symbol.upper():
        price_usd = 1.0 / price_usd if price_usd > 0 else 0

    dex_names = sorted({
        p["dexId"].capitalize()
        for p in filtered
        if float(p.get("liquidity", {}).get("usd", 0) or 0) > 3_000
    })

    return {
        "dex_price":   price_usd,
        "dex_vol_24h": total_vol,
        "dex_liq_usd": total_liq,
        "change_1h":   float(best.get("priceChange", {}).get("h1", 0) or 0),
        "change_6h":   float(best.get("priceChange", {}).get("h6", 0) or 0),
        "change_24h":  float(best.get("priceChange", {}).get("h24", 0) or 0),
        "dex_names":   dex_names,
        "mint":        best.get("baseToken", {}).get("address", ""),
        "pair_url":    best.get("url", ""),
    }


def fetch_dex_by_mint(client: httpx.Client, mint: str, symbol: str) -> dict:
    """Ищет по mint-адресу (точный поиск)."""
    try:
        r = client.get(DEXSCREENER_TOKEN.format(mint))
        if r.status_code != 200:
            return {}
        return _parse_dex_pairs(r.json().get("pairs", []), symbol)
    except Exception as ex:
        print(f" [err mint] {ex}")
        return {}


def fetch_dex_by_symbol(client: httpx.Client, symbol: str) -> dict:
    """Ищет по символу токена (менее точно, но работает без mint)."""
    try:
        r = client.get(DEXSCREENER_SEARCH.format(f"{symbol} USDC"))
        if r.status_code != 200:
            return {}
        return _parse_dex_pairs(r.json().get("pairs", []), symbol)
    except Exception as ex:
        print(f" [err sym] {ex}")
        return {}


# ── основная логика ────────────────────────────────────────────────────────

def run_analysis() -> None:
    print("=" * 72)
    print("  АНАЛИЗ ПАР: Backpack <-> Solana DEX")
    print("=" * 72)

    with _client() as client:
        print("\n[1/3] Загружаем SPOT USDC-рынки Backpack...")
        markets = fetch_backpack_markets(client)
        print(f"      Найдено: {len(markets)} пар")

        print("[2/3] Загружаем тикеры (объёмы, цены)...")
        tickers = fetch_backpack_tickers(client)

        print("[3/3] Проверяем DEX-ликвидность для каждой пары...\n")
        results = []

        for mkt in markets:
            symbol   = mkt["symbol"]          # SOL_USDC
            base_sym = mkt["baseSymbol"]       # SOL

            ticker = tickers.get(symbol, {})
            try:
                bp_vol_usdc = float(ticker.get("quoteVolume", 0) or 0)
                bp_price    = float(ticker.get("lastPrice", 0) or 0)
                bp_high     = float(ticker.get("high", 0) or 0)
                bp_low      = float(ticker.get("low", 0) or 0)
                bp_chg_pct  = float(ticker.get("priceChangePercent", 0) or 0) * 100
            except (ValueError, TypeError):
                bp_vol_usdc = bp_price = bp_high = bp_low = bp_chg_pct = 0.0

            if bp_vol_usdc < MIN_BP_VOL_USD:
                continue

            # дневной swing (high-low)/low
            bp_swing = 0.0
            if bp_high > 0 and bp_low > 0:
                bp_swing = (bp_high - bp_low) / bp_low * 100

            pair_key = f"{base_sym}/USDC"

            # mint: сначала из config, потом ищем
            mint = _SYMBOL_TO_MINT.get(base_sym, "")

            print(f"  {symbol:<22} BP=${bp_vol_usdc:>9,.0f}  swing={bp_swing:>+5.1f}%", end="  ", flush=True)

            if mint:
                dex = fetch_dex_by_mint(client, mint, base_sym)
            else:
                dex = fetch_dex_by_symbol(client, base_sym)
                mint = dex.get("mint", "")

            dex_liq = dex.get("dex_liq_usd", 0)
            dex_vol = dex.get("dex_vol_24h", 0)

            spread_now = 0.0
            dex_price = dex.get("dex_price", 0)
            if bp_price > 0 and dex_price > 0:
                spread_now = abs(bp_price - dex_price) / min(bp_price, dex_price) * 100

            results.append({
                "symbol":     symbol,
                "base_sym":   base_sym,
                "pair_key":   pair_key,
                "in_bot":     pair_key in ALREADY_IN_BOT,
                "bp_price":   bp_price,
                "bp_vol_24h": bp_vol_usdc,
                "bp_swing":   bp_swing,
                "bp_chg_24h": bp_chg_pct,
                "dex_liq":    dex_liq,
                "dex_vol":    dex_vol,
                "change_1h":  dex.get("change_1h", 0),
                "change_6h":  dex.get("change_6h", 0),
                "change_24h": dex.get("change_24h", 0),
                "dex_names":  dex.get("dex_names", []),
                "spread_now": spread_now,
                "mint":       mint,
                "pair_url":   dex.get("pair_url", ""),
            })

            status = "OK " if dex_liq >= MIN_DEX_LIQ_USD else "LOW"
            print(f"DEX liq=${dex_liq:>10,.0f} [{status}]  dexes: {','.join(dex.get('dex_names', [])[:2]) or '—'}")
            time.sleep(0.35)

    # ── фильтр и сортировка ────────────────────────────────────────────────
    qualified = [
        r for r in results
        if r["dex_liq"] >= MIN_DEX_LIQ_USD and r["dex_vol"] >= MIN_DEX_VOL_USD
    ]
    for r in qualified:
        # score = дневной свинг * логарифм объёма (больше = интереснее для арбитража)
        r["score"] = r["bp_swing"] * math.log1p(r["bp_vol_24h"] / 1_000)

    qualified.sort(key=lambda r: r["score"], reverse=True)

    in_bot   = [r for r in qualified if r["in_bot"]]
    new_ones = [r for r in qualified if not r["in_bot"]]
    low_liq  = [r for r in results if r["dex_liq"] < MIN_DEX_LIQ_USD
                and r["bp_vol_24h"] >= MIN_BP_VOL_USD]

    # ── вывод ─────────────────────────────────────────────────────────────
    def print_row(r: dict, idx: int, tag: str = "") -> None:
        dex_str = "/".join(r["dex_names"][:3]) if r["dex_names"] else "нет данных"
        spread_warn = "  <<< СПРЕД!" if r["spread_now"] > 1.0 else ""
        print(
            f"\n  {idx:>2}. [{tag}] {r['symbol']:<20}  score={r.get('score',0):.1f}\n"
            f"      Backpack:  цена={r['bp_price']:.5g}  vol24h=${r['bp_vol_24h']:>10,.0f}\n"
            f"                 swing24h={r['bp_swing']:>+6.2f}%  изм24h={r['bp_chg_24h']:>+6.2f}%\n"
            f"      DEX:       liq=${r['dex_liq']:>10,.0f}  vol24h=${r['dex_vol']:>10,.0f}\n"
            f"                 DEX: {dex_str}\n"
            f"      Изменение: 1ч={r['change_1h']:>+6.2f}%  6ч={r['change_6h']:>+7.2f}%"
            f"  24ч={r['change_24h']:>+7.2f}%\n"
            f"      Спред сейчас: {r['spread_now']:.3f}%{spread_warn}"
            + (f"\n      mint: {r['mint']}" if r['mint'] else "")
        )

    print("\n\n" + "=" * 72)
    print(f"  РЕЗУЛЬТАТЫ  |  Backpack vol>=${MIN_BP_VOL_USD:,}  DEX liq>=${MIN_DEX_LIQ_USD:,}")
    print("=" * 72)

    if in_bot:
        print(f"\n--- УЖЕ В БОТЕ ({len(in_bot)} пар, отсортированы по потенциалу) ---")
        for i, r in enumerate(in_bot, 1):
            print_row(r, i, "В БОТЕ")

    if new_ones:
        print(f"\n\n--- НОВЫЕ КАНДИДАТЫ для добавления ({len(new_ones)} пар) ---")
        for i, r in enumerate(new_ones, 1):
            print_row(r, i, "НОВАЯ ")
    else:
        print("\n\n  Новых кандидатов с нужной ликвидностью не найдено.")

    if low_liq:
        print(f"\n\n--- BACKPACK ЕСТЬ, DEX-ЛИКВИДНОСТЬ СЛАБАЯ (<${MIN_DEX_LIQ_USD:,}) ---")
        print("    (арбитраж возможен, но глубина стакана будет маленькой)")
        for r in sorted(low_liq, key=lambda x: x["bp_vol_24h"], reverse=True)[:10]:
            tag = "В БОТЕ" if r["in_bot"] else "НОВАЯ "
            dex_str = "/".join(r["dex_names"][:2]) or "нет на DEX"
            print(f"    {r['symbol']:<22} BP vol=${r['bp_vol_24h']:>9,.0f}"
                  f"  DEX liq=${r['dex_liq']:>8,.0f}  [{tag}] dex:{dex_str}")

    # ── итог + ТОП-рекомендации ───────────────────────────────────────────
    print("\n\n" + "=" * 72)
    print(f"  ИТОГ:")
    print(f"  Всего SPOT USDC пар на Backpack:       {len(markets)}")
    print(f"  С объёмом > ${MIN_BP_VOL_USD:,} на Backpack:     {len(results)}")
    print(f"  С DEX-ликвидностью > ${MIN_DEX_LIQ_USD:,}:      {len(qualified)}")
    print(f"  Уже в боте: {len(in_bot)}  |  Новых кандидатов: {len(new_ones)}")
    print("=" * 72)

    if new_ones:
        print(f"\n  ТОП-5 рекомендаций для добавления:")
        for r in new_ones[:5]:
            print(f"    + {r['symbol']:<22} swing={r['bp_swing']:>+5.1f}%"
                  f"  BP=${r['bp_vol_24h']:>9,.0f}  DEX liq=${r['dex_liq']:>8,.0f}"
                  f"  {'/'.join(r['dex_names'][:2])}")
        print()
        print("  Чтобы добавить пары в бот — скажи мне и я пропишу mint-адреса в config.py")


if __name__ == "__main__":
    run_analysis()
