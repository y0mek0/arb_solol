"""
config.py — ЕДИНСТВЕННЫЙ источник конфигурации проекта.
Читается всеми модулями; никогда не импортирует другие модули проекта.

Содержит:
  TOKEN_PAIRS      — 18 пар TOKEN/USDC с mint-адресами, decimals, backpack-символами
  MIN_DEPTH_USD    — минимальный объём ($) на ближайшем уровне стакана Backpack
  MAX_SPREAD_PCT   — потолок gross-спреда (выше = стейл/ошибка данных)
  MIN_PROFIT_PCT   — минимальный чистый профит для сигнала (%)
  httpx_proxy()    — строит URL прокси из PROXY_URL / HTTP_PROXY в .env
  httpx_client_kwargs() — единые параметры для httpx.Client (прокси + TLS + timeout)

Все параметры переопределяются через .env (или переменные окружения).
Образец настройки: .env.example

Связь с CONTEXT.md: раздел «Настройка через .env» и «Торговые пары».
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# ── Адреса токенов (Mainnet) ────────────────────────────────────────────────
SOL_MINT   = "So11111111111111111111111111111111111111112"
USDC_MINT  = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT  = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# ── Топ ликвидных Solana-токенов (Backpack + Raydium) ───────────────────────
JUP_MINT    = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"   # Jupiter         6 dec
WETH_MINT   = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"  # wETH Wormhole   8 dec
WBTC_MINT   = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"  # wBTC Wormhole   8 dec
RAY_MINT    = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"  # Raydium         6 dec
BONK_MINT   = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK            5 dec
WIF_MINT    = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"  # dogwifhat       6 dec
JTO_MINT    = "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL"   # Jito            9 dec
PYTH_MINT   = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"  # Pyth Network    6 dec
W_MINT      = "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ"  # Wormhole W      6 dec
MSOL_MINT   = "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"  # Marinade SOL    9 dec
POPCAT_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"  # POPCAT          9 dec
ORCA_MINT   = "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE"   # Orca            6 dec
DRIFT_MINT  = "DriFtupJYLTosbwoN8koMbEYSx54aFAVLddWsbksjwg7"  # Drift           6 dec
HNT_MINT    = "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux"   # Helium          8 dec
RENDER_MINT = "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof"   # Render          6 dec

# Адрес токена BP (Backpack Exchange) — устанавливается в .env после TGE.
BP_TOKEN_MINT: str = os.getenv("BP_TOKEN_MINT", "").strip()
RAYDIUM_POOL_ID: str = os.getenv("RAYDIUM_POOL_ID", "")


# ── Доступные торговые пары (Backpack CEX ↔ Raydium DEX) ───────────────────
# base_decimals: число знаков минимальной единицы токена в сети Solana
def _bp_mint_valid(mint: str) -> bool:
    """Проверяет что mint задан и не является заглушкой."""
    return bool(mint) and mint not in ("FILL_AFTER_TGE", "YOUR_BP_MINT", "") and len(mint) >= 32


def _p(symbol: str, backpack_sym: str, mint: str, base_dec: int, quote_dec: int = 6) -> dict:
    """Вспомогательная функция для краткого определения пары."""
    return {
        "backpack_symbol": backpack_sym,
        "base_mint":       mint,
        "quote_mint":      USDC_MINT,
        "base_decimals":   base_dec,
        "quote_decimals":  quote_dec,
        "base_symbol":     symbol,
    }


def _build_token_pairs() -> dict:
    pairs: dict = {
        # ── Базовые (стабильные объёмы на обоих площадках) ──────────────────
        "SOL/USDC":    _p("SOL",    "SOL_USDC",    SOL_MINT,    9),
        "BTC/USDC":    _p("BTC",    "BTC_USDC",    WBTC_MINT,   8),
        "ETH/USDC":    _p("ETH",    "ETH_USDC",    WETH_MINT,   8),
        # ── DeFi / инфраструктура ────────────────────────────────────────────
        "JUP/USDC":    _p("JUP",    "JUP_USDC",    JUP_MINT,    6),
        "RAY/USDC":    _p("RAY",    "RAY_USDC",    RAY_MINT,    6),
        "JTO/USDC":    _p("JTO",    "JTO_USDC",    JTO_MINT,    9),
        "PYTH/USDC":   _p("PYTH",   "PYTH_USDC",   PYTH_MINT,   6),
        "W/USDC":      _p("W",      "W_USDC",      W_MINT,      6),
        "MSOL/USDC":   _p("MSOL",   "MSOL_USDC",   MSOL_MINT,   9),
        "ORCA/USDC":   _p("ORCA",   "ORCA_USDC",   ORCA_MINT,   6),
        "DRIFT/USDC":  _p("DRIFT",  "DRIFT_USDC",  DRIFT_MINT,  6),
        "HNT/USDC":    _p("HNT",    "HNT_USDC",    HNT_MINT,    8),
        "RENDER/USDC": _p("RENDER", "RENDER_USDC", RENDER_MINT, 6),
        # ── Мемкоины (высокая волатильность = больше арб-возможностей) ──────
        "BONK/USDC":   _p("BONK",   "BONK_USDC",   BONK_MINT,   5),
        "WIF/USDC":    _p("WIF",    "WIF_USDC",    WIF_MINT,    6),
        "POPCAT/USDC": _p("POPCAT", "POPCAT_USDC", POPCAT_MINT, 9),
        # ── Стейбл-стейбл (спред CEX↔DEX даже ~0.1% = прибыль) ─────────────
        "USDT/USDC":   _p("USDT",   "USDT_USDC",   USDT_MINT,   6),
    }
    if _bp_mint_valid(BP_TOKEN_MINT):
        pairs["BP/USDC"] = _p("BP", "BP_USDC", BP_TOKEN_MINT, 6)
    else:
        pairs["BP/USDC"] = _p("BP", "BP_USDC", "", 6)
    return pairs


TOKEN_PAIRS: dict = _build_token_pairs()
DEFAULT_PAIR: str = os.getenv("DEFAULT_PAIR", "SOL/USDC")

# ── Сеть ────────────────────────────────────────────────────────────────────
RPC_URL: str = os.getenv(
    "RPC_URL",
    "https://api.mainnet-beta.solana.com"   # запасной публичный RPC
)

# ── Кошелёк ─────────────────────────────────────────────────────────────────
WALLET_PRIVATE_KEY: str = os.getenv("WALLET_PRIVATE_KEY", "")

# Backpack signed API (ордера) — https://docs.backpack.exchange/
# Тикер публичный и ключей не требует; эти поля для будущего/ручной интеграции.
BACKPACK_API_KEY: str = os.getenv("BACKPACK_API_KEY", "").strip()
BACKPACK_API_SECRET: str = os.getenv("BACKPACK_API_SECRET", "").strip()

# ── Торговые параметры ───────────────────────────────────────────────────────
# Минимальный чистый профит для входа (%)
MIN_PROFIT_PCT: float = float(os.getenv("MIN_PROFIT_PCT", "0.5"))

# Минимальный объём (USD) на ближайшем уровне стакана Backpack
# Если лучший bid или ask тоньше — сигнал не считается прибыльным (нет ликвидности под сделку)
MIN_DEPTH_USD: float = float(os.getenv("MIN_DEPTH_USD", "30"))

# Максимально допустимый gross-спред (%).
# Спреды выше этого порога = стейл/нерабочий рынок (разные активы, устаревшие ордера, ошибки API).
# Реальный арбитраж на ликвидных парах не превышает 5-10%; 25% — жёсткий потолок здравого смысла.
MAX_SPREAD_PCT: float = float(os.getenv("MAX_SPREAD_PCT", "25"))

# Размер одной сделки в USDC
TRADE_AMOUNT_USDC: float = float(os.getenv("TRADE_AMOUNT_USDC", "10"))

# Slippage в базисных пунктах (1 bps = 0.01%)
SLIPPAGE_BPS: int = int(os.getenv("SLIPPAGE_BPS", "200"))

# Интервал опроса цен (сек)
POLL_INTERVAL_SEC: float = float(os.getenv("POLL_INTERVAL_SEC", "3"))

# ── Комиссии Solana ──────────────────────────────────────────────────────────
# Базовая сетевая комиссия (в SOL)
BASE_TX_FEE_SOL: float = 0.000005

# Priority fee — доплата за скорость (в SOL, берём консервативное значение)
PRIORITY_FEE_SOL: float = 0.002

# Итоговые сетевые расходы на одну транзакцию (в SOL)
NETWORK_FEE_SOL: float = BASE_TX_FEE_SOL + PRIORITY_FEE_SOL

# ── Комиссии DEX ─────────────────────────────────────────────────────────────
RAYDIUM_FEE_PCT: float = 0.0025   # 0.25% за своп на Raydium

# Backpack (CEX) — оценка taker; подставь свои фактические % из правил биржи
BACKPACK_TAKER_FEE_PCT: float = float(os.getenv("BACKPACK_TAKER_FEE_PCT", "0.001"))

# Сколько on-chain транзакций Solana ожидаем на один круг (CEX+DEX обычно 1 своп в сети)
ARBIT_ESTIMATED_SOL_TX_COUNT: int = int(os.getenv("ARBIT_ESTIMATED_SOL_TX_COUNT", "1"))

# ── Backpack Exchange (публичный REST, без ключей) ───────────────────────────
BACKPACK_API_BASE: str = os.getenv("BACKPACK_API_BASE", "https://api.backpack.exchange")
# Символы спота на Backpack (формат BASE_QUOTE)
BACKPACK_SYMBOL_SOL_USDC: str = os.getenv("BACKPACK_SYMBOL_SOL_USDC", "SOL_USDC")
BACKPACK_SYMBOL_BP_USDC: str = os.getenv("BACKPACK_SYMBOL_BP_USDC", "BP_USDC")

# ── API endpoints ─────────────────────────────────────────────────────────────
JUPITER_QUOTE_URL  = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL   = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE_URL  = "https://price.jup.ag/v6/price"
RAYDIUM_API_URL    = "https://api.raydium.io/v2"

# ── Режим dry-run (симуляция без отправки транзакций) ────────────────────────
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

# Авто-своп в сети при арбитраже Backpack↔Raydium (по умолчанию выкл.: нужен Backpack API + продажа на Raydium = отдельный quote)
ENABLE_CHAIN_EXECUTE: bool = os.getenv("ENABLE_CHAIN_EXECUTE", "false").lower() == "true"

# Прокси для httpx (любой из вариантов):
#   PROXY_URL=socks5://user:pass@host:port
#   PROXY_URL=https://user:pass@host:port
#   PROXY_URL=http://user:pass@host:port
# Для socks5 нужен пакет httpx[socks] (уже в requirements.txt).
PROXY_URL: str = os.getenv("PROXY_URL", "").strip()
HTTP_PROXY: str = os.getenv("HTTP_PROXY", "").strip()  # запасной вариант, если PROXY_URL пуст


def _normalize_proxy_url(u: str) -> str:
    """
    httpx 0.27 + httpcore не знают схему socks5h:// → Unknown scheme.
    Конвертируем в socks5:// (DNS тогда локальный; при SSL EOF см. README / HTTPX_VERIFY_SSL).
    """
    u = u.strip()
    if u.lower().startswith("socks5h://"):
        return "socks5://" + u[10:]
    return u


def httpx_proxy() -> str | None:
    """Один URL для всех исходящих HTTPS-запросов через httpx."""
    u = PROXY_URL or HTTP_PROXY
    if not u:
        return None
    return _normalize_proxy_url(u)


def proxy_label_safe() -> str:
    """Короткая подпись без логина/пароля (для GUI)."""
    from urllib.parse import urlparse

    u = PROXY_URL or HTTP_PROXY
    if not u:
        return ""
    u = _normalize_proxy_url(u)
    try:
        p = urlparse(u)
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{host}{port}"
    except Exception:
        return "(proxy)"


# Проверка TLS для httpx. При SSL EOF через SOCKS5 попробуй HTTPX_VERIFY_SSL=false (только если доверяешь прокси).
HTTPX_VERIFY_SSL: bool = os.getenv("HTTPX_VERIFY_SSL", "true").lower() in ("1", "true", "yes")


def httpx_client_kwargs(timeout: float = 25.0) -> dict:
    """Общие параметры для httpx.Client / AsyncClient."""
    kw: dict = {"timeout": timeout, "verify": HTTPX_VERIFY_SSL}
    p = httpx_proxy()
    if p:
        kw["proxy"] = p
    return kw


# Backpack GET /api/v1/depth — limit только из этого набора (иначе 400 Bad Request)
BACKPACK_DEPTH_LIMITS: tuple[int, ...] = (5, 10, 20, 50, 100, 500, 1000)


def backpack_depth_limit_param(requested: int) -> str:
    """Ближайшее допустимое значение limit для API Backpack."""
    if requested <= 0:
        return "20"
    for v in BACKPACK_DEPTH_LIMITS:
        if v >= requested:
            return str(v)
    return "1000"


# Желаемое число уровней стакана (будет округлено к 5/10/20/…)
ORDERBOOK_LEVELS: int = int(os.getenv("ORDERBOOK_LEVELS", "20"))
