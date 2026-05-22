"""
wallet.py — Solana-кошелёк: загрузка keypair и on-chain балансы.

Публичные функции:
  load_keypair() → Keypair | None
      Загружает приватный ключ из WALLET_PRIVATE_KEY в .env (формат base58 или JSON-массив).
      Используется в main.py (CLI) и market_data.py (для on-chain балансов в GUI).

  get_sol_balance(pubkey_str) → float
      SOL-баланс (lamports → SOL) через RPC_URL из .env.
      Публичный RPC: https://api.mainnet-beta.solana.com (по умолчанию).
      ВАЖНО: Helius и другие кастомные RPC без ключа дают 401 → баланс 0.

  get_usdc_balance(pubkey_str) → float
      USDC-баланс (SPL-токен) через getTokenAccountsByOwner.

  get_sol_price_usd() → float
      Цена SOL в USD через CoinGecko (fallback) или Raydium v3.
      Используется в main.py для расчёта network_fee.

Связь с CONTEXT.md: разделы «Частые проблемы» (SOL баланс 0) и «Файлы».
"""

import json
import base58
from typing import Optional

from solders.keypair import Keypair  # type: ignore
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts
from solders.pubkey import Pubkey  # type: ignore

import config
from logger import log


def load_keypair() -> Optional[Keypair]:
    """
    Загружает Keypair из переменной окружения WALLET_PRIVATE_KEY.
    Поддерживает два формата:
      - base58 строка (стандарт Phantom/Backpack)
      - JSON массив байт (формат Solana CLI)
    """
    raw = config.WALLET_PRIVATE_KEY.strip()
    if not raw:
        log.error("WALLET_PRIVATE_KEY не задан в .env!")
        return None

    try:
        # Пробуем JSON формат: [1, 2, 3, ...]
        if raw.startswith("["):
            byte_list = json.loads(raw)
            return Keypair.from_bytes(bytes(byte_list))

        # Пробуем base58
        decoded = base58.b58decode(raw)
        return Keypair.from_bytes(decoded)

    except Exception as e:
        log.error(f"Ошибка загрузки keypair: {e}")
        return None


async def get_sol_balance(pubkey: str) -> float:
    """Возвращает баланс SOL кошелька."""
    try:
        async with AsyncClient(config.RPC_URL) as client:
            resp = await client.get_balance(Pubkey.from_string(pubkey))
            lamports = resp.value
            return lamports / 1e9
    except Exception as e:
        log.warning(f"get_sol_balance error: {e}")
        return 0.0


async def get_token_balance(pubkey: str, mint: str) -> float:
    """
    Возвращает баланс SPL-токена.
    Ищет первый ATA (Associated Token Account) для данного mint.
    """
    try:
        async with AsyncClient(config.RPC_URL) as client:
            opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
            resp = await client.get_token_accounts_by_owner(
                Pubkey.from_string(pubkey), opts
            )
            accounts = resp.value
            if not accounts:
                return 0.0

            # Берём первый подходящий счёт
            account_info = await client.get_token_account_balance(
                accounts[0].pubkey
            )
            amount = account_info.value.ui_amount
            return float(amount) if amount is not None else 0.0
    except Exception as e:
        log.warning(f"get_token_balance error (mint={mint}): {e}")
        return 0.0


async def get_sol_price_usd() -> float:
    """
    Получает текущую цену SOL в USD через Jupiter Price API.
    Используется для расчёта сетевых комиссий в USD.
    """
    import httpx
    try:
        async with httpx.AsyncClient(**config.httpx_client_kwargs(timeout=15.0)) as client:
            resp = await client.get(
                config.JUPITER_PRICE_URL,
                params={"ids": config.SOL_MINT},
            )
            resp.raise_for_status()
            data = resp.json()
            price = data["data"][config.SOL_MINT]["price"]
            return float(price)
    except Exception as e:
        log.warning(f"get_sol_price_usd error: {e}, используем 150.0$")
        return 150.0
