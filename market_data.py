"""
market_data.py — оркестратор данных для GUI. Центральный модуль сбора и расчёта.

Два основных публичных интерфейса:

  build_snapshot(**kwargs) → MarketSnapshot
      Полный снимок для ОДНОЙ активной пары (вкладка Мониторинг):
      - Тикер Backpack + глубина стакана
      - Цена Raydium v3 + DexScreener (Orca/Meteora/Phoenix)
      - effective_bp_prices() — первый уровень стакана >= MIN_DEPTH_USD
      - detect_arbitrage() для каждого DEX × каждого направления
      - On-chain балансы + балансы Backpack
      Вызывается из gui_app._refresh_async() в ThreadPoolExecutor.

  scan_all_pairs_light(sol_price_usd) → list[LightSignal]
      Быстрый параллельный скан ВСЕХ 18 пар (фоновый поток GUI):
      - ThreadPoolExecutor(max_workers=N) — все пары одновременно
      - Для каждой пары: тикер + Raydium v3 + DexScreener + depth(limit=20)
      - Возвращает LightSignal для каждой пары (прибыльный или информационный)
      Вызывается из gui_app._start_background_scanner() каждые 8 сек.

Ключевые фильтры (применяются здесь и в arbitrage_detector):
  - effective_bp_prices(): пропускаем копеечные ордера, берём eff_bid/eff_ask
  - MIN_DEPTH_USD: минимальный объём уровня стакана
  - MAX_SPREAD_PCT: потолок спреда (в detector)

Все HTTP-запросы синхронные (httpx.Client), вызываются из background-потоков GUI.
Связь с CONTEXT.md: раздел «Архитектура и поток данных».
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from solders.pubkey import Pubkey  # type: ignore
from solana.rpc.api import Client as SolanaClient

import config
from arbitrage_detector import detect_arbitrage
from backpack_private import get_capital_balances
from dex_prices import best_buy_price, best_sell_price, fetch_token_dex_prices
from price_monitor import Price, RaydiumLegMode, price_from_backpack_ticker, raydium_quote_to_price
from proxy_monitor import monitor as _prx
from raydium_api import get_raydium_price, get_sol_price_from_raydium


def _httpx_kwargs(timeout: float = 25.0) -> dict:
    return config.httpx_client_kwargs(timeout=timeout)


def _timed_get(client: httpx.Client, url: str, source: str, **kwargs):
    """httpx GET с записью в proxy_monitor."""
    t0 = time.time()
    try:
        r = client.get(url, **kwargs)
        _prx.record(success=True, source=source, latency_ms=(time.time()-t0)*1000)
        return r
    except Exception as e:
        _prx.record(success=False, source=source, latency_ms=(time.time()-t0)*1000,
                    error=type(e).__name__ + ": " + str(e)[:80])
        raise


def fetch_jupiter_raydium_quote_sync(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
) -> tuple[Optional[dict], Optional[str]]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(config.SLIPPAGE_BPS),
        "dexes": "Raydium",
    }
    try:
        with httpx.Client(**_httpx_kwargs()) as c:
            r = c.get(config.JUPITER_QUOTE_URL, params=params)
            if r.status_code != 200:
                return None, f"Jupiter HTTP {r.status_code}: {r.text[:200]}"
            return r.json(), None
    except OSError as e:
        # WinError 11001 getaddrinfo failed
        return None, f"Сеть/DNS: {e!s} (интернет, VPN; в .env PROXY_URL=socks5:// или https://)"
    except Exception as e:
        return None, str(e)


def fetch_backpack_ticker_sync(symbol: str) -> tuple[Optional[dict], Optional[str]]:
    url = f"{config.BACKPACK_API_BASE.rstrip('/')}/api/v1/ticker"
    try:
        with httpx.Client(**_httpx_kwargs()) as c:
            r = _timed_get(c, url, f"BP ticker {symbol}", params={"symbol": symbol})
            if r.status_code == 204:
                return None, f"Рынок не найден: {symbol}"
            r.raise_for_status()
            return r.json(), None
    except Exception as e:
        return None, str(e)


def fetch_backpack_depth_sync(symbol: str, limit: int = 20) -> tuple[Optional[dict], Optional[str]]:
    """Backpack depth: limit строго из набора 5/10/20/50/100/500/1000."""
    url = f"{config.BACKPACK_API_BASE.rstrip('/')}/api/v1/depth"
    lim = config.backpack_depth_limit_param(limit)
    try:
        with httpx.Client(**_httpx_kwargs()) as c:
            r = _timed_get(c, url, f"BP depth {symbol}", params={"symbol": symbol, "limit": lim})
            r.raise_for_status()
            return r.json(), None
    except Exception as e:
        return None, str(e)


def fetch_sol_price_usd_sync() -> tuple[float, Optional[str]]:
    # 1. Raydium v3 mint/price
    try:
        p, err = get_sol_price_from_raydium()
        if p and p > 0:
            _prx.record(success=True, source="Raydium mint/price")
            return p, None
        _prx.record(success=False, source="Raydium mint/price", error=str(err))
    except Exception as e:
        _prx.record(success=False, source="Raydium mint/price", error=str(e))

    # 2. Jupiter Price API
    j_err: Optional[str] = None
    try:
        with httpx.Client(**_httpx_kwargs()) as c:
            r = _timed_get(c, config.JUPITER_PRICE_URL, "Jupiter price",
                           params={"ids": config.SOL_MINT})
            r.raise_for_status()
            p = float(r.json()["data"][config.SOL_MINT]["price"])
            return p, None
    except Exception as e:
        j_err = str(e)

    # 3. CoinGecko
    try:
        with httpx.Client(**_httpx_kwargs()) as c:
            r = _timed_get(c, "https://api.coingecko.com/api/v3/simple/price",
                           "CoinGecko",
                           params={"ids": "solana", "vs_currencies": "usd"})
            r.raise_for_status()
            p = float(r.json()["solana"]["usd"])
            return p, "CoinGecko (Jupiter недоступен)" if j_err else None
    except Exception as e2:
        return 150.0, f"SOL=150 fallback. Jupiter: {j_err}; CoinGecko: {e2}"


def _first_liquid_idx(levels: list, min_usd: float) -> int:
    """
    Индекс первого уровня стакана с объёмом (price × qty) >= min_usd.
    Возвращает -1, если ни один уровень не достигает порога.
    bids: отсортированы по убыванию цены; asks: по возрастанию.
    """
    if min_usd <= 0:
        return 0
    for i, lvl in enumerate(levels):
        try:
            if float(lvl[0]) * float(lvl[1]) >= min_usd:
                return i
        except Exception:
            continue
    return -1


def effective_bp_prices(
    depth: Optional[dict],
    min_usd: float,
) -> tuple[Optional[float], Optional[float], int, int]:
    """
    Возвращает (eff_bid_price, eff_ask_price, bid_idx, ask_idx).

    eff_bid_price — цена ПЕРВОГО bid-уровня с объёмом >= min_usd.
    eff_ask_price — цена ПЕРВОГО ask-уровня с объёмом >= min_usd.
    *_idx        — индексы этих уровней в соответствующих списках (-1 = не найден).

    Именно эти цены нужно использовать для расчёта арбитража —
    всё что выше/ниже них — «копеечные» ордера без реального объёма.
    """
    if not depth:
        return None, None, -1, -1

    bids = depth.get("bids", [])
    asks = depth.get("asks", [])

    b_idx = _first_liquid_idx(bids, min_usd)
    a_idx = _first_liquid_idx(asks, min_usd)

    eff_bid = float(bids[b_idx][0]) if b_idx >= 0 else None
    eff_ask = float(asks[a_idx][0]) if a_idx >= 0 else None

    return eff_bid, eff_ask, b_idx, a_idx


def format_depth_levels(
    side: str,
    levels: list,
    *,
    is_bid: bool,
    max_rows: int,
    liquid_idx: int = -1,
) -> tuple[list[tuple[str, str, str, bool]], float]:
    """
    levels: [["price","qty"], ...] в API Backpack.
    liquid_idx: индекс первого ликвидного уровня (помечается флагом True в кортеже).
    Возвращает строки (price, qty, usdc, is_first_liquid) и суммарный объём в USDC.
    """
    rows: list[tuple[str, str, str, bool]] = []
    cum_quote = 0.0
    for i, row in enumerate(levels[:max_rows]):
        if len(row) < 2:
            continue
        p, q = float(row[0]), float(row[1])
        quote = p * q
        cum_quote += quote
        rows.append((f"{p:.4f}", f"{q:.6f}", f"{quote:,.2f}", i == liquid_idx))
    return rows, cum_quote


def get_on_chain_balances(pubkey_str: str) -> tuple[dict[str, float], Optional[str]]:
    out: dict[str, float] = {"SOL": 0.0, "USDC": 0.0}
    if not pubkey_str:
        return out, "Кошелёк не задан"
    try:
        cli = SolanaClient(config.RPC_URL)
        pk = Pubkey.from_string(pubkey_str)
        lamports = cli.get_balance(pk).value
        out["SOL"] = lamports / 1e9

        from solana.rpc.types import TokenAccountOpts

        opts = TokenAccountOpts(mint=Pubkey.from_string(config.USDC_MINT))
        resp = cli.get_token_accounts_by_owner(pk, opts)
        if resp.value:
            bal = cli.get_token_account_balance(resp.value[0].pubkey)
            if bal.value.ui_amount is not None:
                out["USDC"] = float(bal.value.ui_amount)
        return out, None
    except Exception as e:
        return out, str(e)


@dataclass
class MarketSnapshot:
    time_iso: str = ""
    backpack_symbol: str = ""
    base_mint: str = ""
    quote_mint: str = ""

    backpack_usdc_per_base: Optional[float] = None
    raydium_usdc_per_base: Optional[float] = None
    backpack_err: Optional[str] = None
    raydium_err: Optional[str] = None

    backpack_price: Optional[Price] = None
    raydium_price: Optional[Price] = None

    spread_pct: Optional[float] = None
    opportunity_profitable: bool = False
    opportunity_text: str = ""

    sol_price_usd: float = 150.0
    sol_price_note: Optional[str] = None

    depth_bid_rows: list = field(default_factory=list)
    depth_ask_rows: list = field(default_factory=list)
    depth_bid_liquidity_usdc: float = 0.0
    depth_ask_liquidity_usdc: float = 0.0
    depth_err: Optional[str] = None

    raydium_impact_pct: Optional[str] = None
    raydium_route: str = ""

    on_chain: dict = field(default_factory=dict)
    on_chain_err: Optional[str] = None
    backpack_balances: Optional[dict] = None
    backpack_balance_err: Optional[str] = None

    wallet_address: str = ""
    pair_key: str = ""               # ключ из TOKEN_PAIRS — для отсева устаревших результатов
    net_profit_usd: Optional[float] = None   # чистый профит из сигнала (USD)
    net_profit_pct: Optional[float] = None   # чистый профит из сигнала (%)

    # Первые ликвидные уровни стакана (>= MIN_DEPTH_USD)
    eff_bp_bid_price: Optional[float] = None   # реальная цена продажи на Backpack
    eff_bp_ask_price: Optional[float] = None   # реальная цена покупки на Backpack
    eff_bid_depth_idx: int = -1                # индекс строки в depth_bid_rows
    eff_ask_depth_idx: int = -1                # индекс строки в depth_ask_rows

    # Многодексовые цены (Raydium, Orca, Meteora, Phoenix)
    dex_prices:   dict = field(default_factory=dict)  # {dex_name: price_usdc}
    best_dex_name: str = "Raydium"


def build_snapshot(
    *,
    backpack_symbol: str,
    base_mint: str,
    quote_mint: str,
    raydium_input_mint: str,
    raydium_output_mint: str,
    raydium_amount_raw: int,
    raydium_in_decimals: int,
    raydium_out_decimals: int,
    wallet_pubkey: str = "",
) -> MarketSnapshot:
    snap = MarketSnapshot(
        backpack_symbol=backpack_symbol,
        base_mint=base_mint,
        quote_mint=quote_mint,
        wallet_address=wallet_pubkey,
    )
    snap.time_iso = time.strftime("%H:%M:%S")

    tick, terr = fetch_backpack_ticker_sync(backpack_symbol)
    if tick:
        snap.backpack_price = price_from_backpack_ticker(
            tick, base_mint=base_mint, quote_mint=quote_mint, symbol=backpack_symbol
        )
        if snap.backpack_price:
            snap.backpack_usdc_per_base = snap.backpack_price.price
    snap.backpack_err = terr

    # Raydium v3 API — основной источник (работает через SOCKS5)
    ray_price_v3, ray_err_v3 = get_raydium_price(base_mint, quote_mint)
    if ray_price_v3:
        snap.raydium_price = ray_price_v3
        snap.raydium_usdc_per_base = ray_price_v3.price
        snap.raydium_route = ray_price_v3.route_label
        snap.raydium_err = None
    else:
        # Fallback: Jupiter Quote API
        jq, jerr = fetch_jupiter_raydium_quote_sync(
            raydium_input_mint, raydium_output_mint, raydium_amount_raw
        )
        if jq:
            snap.raydium_price = raydium_quote_to_price(
                jq,
                RaydiumLegMode.QUOTE_IN_BASE_OUT,
                base_mint,
                quote_mint,
                raydium_in_decimals,
                raydium_out_decimals,
            )
            if snap.raydium_price:
                snap.raydium_usdc_per_base = snap.raydium_price.price
            snap.raydium_impact_pct = jq.get("priceImpactPct")
            for step in jq.get("routePlan", [])[:3]:
                snap.raydium_route += step.get("swapInfo", {}).get("label", "?") + " → "
            snap.raydium_route = snap.raydium_route.rstrip(" → ")
        snap.raydium_err = f"Raydium v3: {ray_err_v3}; Jupiter: {jerr}" if jerr else ray_err_v3

    sp, sp_note = fetch_sol_price_usd_sync()
    snap.sol_price_usd = sp
    snap.sol_price_note = sp_note

    dep, derr = fetch_backpack_depth_sync(backpack_symbol, limit=config.ORDERBOOK_LEVELS)
    if dep:
        bids = dep.get("bids") or []
        asks = dep.get("asks") or []
        lv = config.ORDERBOOK_LEVELS
        (snap.eff_bp_bid_price, snap.eff_bp_ask_price,
         snap.eff_bid_depth_idx, snap.eff_ask_depth_idx) = effective_bp_prices(dep, config.MIN_DEPTH_USD)
        snap.depth_bid_rows, snap.depth_bid_liquidity_usdc = format_depth_levels(
            "bid", bids, is_bid=True, max_rows=lv, liquid_idx=snap.eff_bid_depth_idx)
        snap.depth_ask_rows, snap.depth_ask_liquidity_usdc = format_depth_levels(
            "ask", asks, is_bid=False, max_rows=lv, liquid_idx=snap.eff_ask_depth_idx)
    snap.depth_err = derr

    # ── Загружаем цены со всех DEX (Raydium точный v3 + DexScreener для остальных) ──
    from arbitrage_detector import Direction
    from dataclasses import replace as _dc_replace

    all_dex = fetch_token_dex_prices(base_mint, quote_mint)
    if snap.raydium_usdc_per_base:
        all_dex["Raydium"] = snap.raydium_usdc_per_base   # точный v3 приоритетнее
    snap.dex_prices = dict(all_dex)

    # ── Арбитраж по ЭФФЕКТИВНЫМ ценам Backpack × всех DEX ──────────────────
    sig_candidates: list = []  # (signal, dex_name)

    for dex_name, dex_p in all_dex.items():
        if not dex_p or not snap.backpack_price or not snap.raydium_price:
            continue
        dex_price_obj = _dc_replace(snap.raydium_price, price=dex_p)

        if snap.eff_bp_bid_price:
            bp_eff = _dc_replace(snap.backpack_price, price=snap.eff_bp_bid_price)
            s = detect_arbitrage(bp_eff, dex_price_obj,
                                 trade_amount_usd=config.TRADE_AMOUNT_USDC,
                                 sol_price_usd=snap.sol_price_usd)
            if s and s.direction == Direction.BUY_RAYDIUM_SELL_BACKPACK:
                sig_candidates.append((s, dex_name))

        if snap.eff_bp_ask_price:
            bp_eff = _dc_replace(snap.backpack_price, price=snap.eff_bp_ask_price)
            s = detect_arbitrage(bp_eff, dex_price_obj,
                                 trade_amount_usd=config.TRADE_AMOUNT_USDC,
                                 sol_price_usd=snap.sol_price_usd)
            if s and s.direction == Direction.BUY_BACKPACK_SELL_RAYDIUM:
                sig_candidates.append((s, dex_name))

    best_pair = max(sig_candidates, key=lambda x: x[0].net_profit_pct) if sig_candidates else None
    sig = best_pair[0] if best_pair else None
    snap.best_dex_name = best_pair[1] if best_pair else "Raydium"

    if sig:
        snap.spread_pct = sig.gross_spread_pct
        snap.net_profit_usd = sig.net_profit_usd
        snap.net_profit_pct = sig.net_profit_pct
        snap.opportunity_profitable = sig.is_profitable
        snap.opportunity_text = f"[{snap.best_dex_name}] {sig}"
        if not snap.eff_bp_bid_price and not snap.eff_bp_ask_price:
            snap.opportunity_profitable = False
            snap.opportunity_text += f"  ⚠ нет уровней >= ${config.MIN_DEPTH_USD:.0f} в стакане"
    elif snap.backpack_usdc_per_base and snap.raydium_usdc_per_base:
        b, r = snap.backpack_usdc_per_base, snap.raydium_usdc_per_base
        snap.spread_pct = (max(b, r) - min(b, r)) / min(b, r) * 100
        snap.opportunity_text = f"Спред ~{snap.spread_pct:.3f}% (ниже порога)"
    else:
        snap.opportunity_text = "Нужны обе цены (проверь Raydium/Jupiter и сеть)."

    if wallet_pubkey:
        bal, berr = get_on_chain_balances(wallet_pubkey)
        snap.on_chain = bal
        snap.on_chain_err = berr

    cap, cerr = get_capital_balances()
    snap.backpack_balances = cap
    snap.backpack_balance_err = cerr

    return snap


# ── Лёгкий скан всех пар (для фонового мониторинга) ────────────────────────

@dataclass
class LightSignal:
    """Лёгкий арбитражный сигнал — только цены и расчёт, без стакана и балансов."""
    pair_key: str
    backpack_symbol: str
    base_symbol: str
    time_str: str
    bp_price: float
    ray_price: float           # цена лучшего DEX (не только Raydium)
    spread_pct: float
    is_profitable: bool
    net_profit_usd: float
    net_profit_pct: float
    direction: str             # "Backpack->DEX" или "DEX->Backpack"
    trade_amount_usd: float
    dex_name: str = "Raydium"                       # какой DEX даёт лучшую цену
    all_dex_prices: dict = field(default_factory=dict)  # {dex: price}


def _scan_one_pair(pair_key: str, pair_cfg: dict, sol_price_usd: float) -> Optional[LightSignal]:
    """Сканирует одну пару. Возвращает LightSignal если есть сигнал (прибыльный или нет)."""
    base_mint = pair_cfg.get("base_mint", "")
    if not base_mint:
        return None
    quote_mint = pair_cfg.get("quote_mint", config.USDC_MINT)
    bp_sym = pair_cfg["backpack_symbol"]

    tick, _ = fetch_backpack_ticker_sync(bp_sym)
    if not tick:
        return None
    bp_price_obj = price_from_backpack_ticker(
        tick, base_mint=base_mint, quote_mint=quote_mint, symbol=bp_sym
    )
    if not bp_price_obj:
        return None

    ray_price_obj, _ = get_raydium_price(base_mint, quote_mint)
    ray_p_v3 = ray_price_obj.price if ray_price_obj else None

    # Получаем цены со всех DEX (DexScreener) + переопределяем Raydium точным v3 значением
    all_dex = fetch_token_dex_prices(base_mint, quote_mint)
    if ray_p_v3:
        all_dex["Raydium"] = ray_p_v3

    if not all_dex:
        return None   # нет данных ни с одного DEX

    # Загружаем стакан Backpack для эффективных цен (без копеечных ордеров)
    dep_data, _ = fetch_backpack_depth_sync(bp_sym, limit=20)
    eff_bid, eff_ask, _, _ = effective_bp_prices(dep_data, config.MIN_DEPTH_USD)

    from arbitrage_detector import Direction
    from dataclasses import replace as _dc_replace

    bp_p = bp_price_obj.price

    # Пробуем каждый DEX × каждое направление, берём лучшую возможность
    sig_candidates: list = []  # (signal, direction_str, dex_name, dex_price)

    for dex_name, dex_p in all_dex.items():
        if not dex_p or not ray_price_obj:
            continue
        dex_price_obj = _dc_replace(ray_price_obj, price=dex_p) if ray_price_obj else None
        if not dex_price_obj:
            continue

        if eff_bid:
            bp_eff = _dc_replace(bp_price_obj, price=eff_bid)
            s = detect_arbitrage(bp_eff, dex_price_obj,
                                 trade_amount_usd=config.TRADE_AMOUNT_USDC,
                                 sol_price_usd=sol_price_usd)
            if s and s.direction == Direction.BUY_RAYDIUM_SELL_BACKPACK:
                sig_candidates.append((s, f"{dex_name}->Backpack", dex_name, dex_p))

        if eff_ask:
            bp_eff = _dc_replace(bp_price_obj, price=eff_ask)
            s = detect_arbitrage(bp_eff, dex_price_obj,
                                 trade_amount_usd=config.TRADE_AMOUNT_USDC,
                                 sol_price_usd=sol_price_usd)
            if s and s.direction == Direction.BUY_BACKPACK_SELL_RAYDIUM:
                sig_candidates.append((s, f"Backpack->{dex_name}", dex_name, dex_p))

    if sig_candidates:
        sig, direction, best_dex, best_dex_p = max(
            sig_candidates, key=lambda x: x[0].net_profit_pct
        )
        bp_eff_p = (eff_ask or bp_p) if direction.startswith("Backpack") else (eff_bid or bp_p)
        return LightSignal(
            pair_key=pair_key,
            backpack_symbol=bp_sym,
            base_symbol=pair_cfg.get("base_symbol", "?"),
            time_str=time.strftime("%H:%M:%S"),
            bp_price=bp_eff_p,
            ray_price=best_dex_p,
            spread_pct=sig.gross_spread_pct,
            is_profitable=sig.is_profitable,
            net_profit_usd=sig.net_profit_usd,
            net_profit_pct=sig.net_profit_pct,
            direction=direction,
            trade_amount_usd=sig.trade_amount_usd,
            dex_name=best_dex,
            all_dex_prices=all_dex,
        )

    # Нет прибыльной возможности — показываем raw-спред относительно лучшего DEX
    best_dex_p_raw = max(all_dex.values())
    best_dex_raw   = max(all_dex, key=lambda k: all_dex[k])
    if bp_p > 0 and best_dex_p_raw > 0:
        raw_spread = (max(bp_p, best_dex_p_raw) - min(bp_p, best_dex_p_raw)) / min(bp_p, best_dex_p_raw) * 100
        no_depth = not eff_bid and not eff_ask
        dir_base = f"Backpack->{best_dex_raw}" if bp_p < best_dex_p_raw else f"{best_dex_raw}->Backpack"
        direction = dir_base + (" [нет стакана >= $" + f"{config.MIN_DEPTH_USD:.0f}]" if no_depth else "")
        return LightSignal(
            pair_key=pair_key,
            backpack_symbol=bp_sym,
            base_symbol=pair_cfg.get("base_symbol", "?"),
            time_str=time.strftime("%H:%M:%S"),
            bp_price=bp_p,
            ray_price=best_dex_p_raw,
            spread_pct=raw_spread,
            is_profitable=False,
            net_profit_usd=0.0,
            net_profit_pct=0.0,
            direction=direction,
            trade_amount_usd=config.TRADE_AMOUNT_USDC,
            dex_name=best_dex_raw,
            all_dex_prices=all_dex,
        )
    return None


def scan_all_pairs_light(sol_price_usd: float = 150.0) -> list[LightSignal]:
    """
    Параллельно сканирует все TOKEN_PAIRS.
    Возвращает список LightSignal (все пары, не только прибыльные).
    """
    pairs = {k: v for k, v in config.TOKEN_PAIRS.items() if v.get("base_mint")}
    results: list[LightSignal] = []
    with ThreadPoolExecutor(max_workers=len(pairs) or 1) as pool:
        futures = {
            pool.submit(_scan_one_pair, k, v, sol_price_usd): k
            for k, v in pairs.items()
        }
        for fut in as_completed(futures, timeout=30):
            try:
                sig = fut.result()
                if sig is not None:
                    results.append(sig)
            except Exception:
                pass
    return results
