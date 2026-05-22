"""
executor.py — исполнение DEX-свопов (DEX-нога арбитража).

ТЕКУЩИЙ СТАТУС: реализован, но НЕ используется в GUI-режиме автоматически.
Вызывается только из main.py (CLI) при ENABLE_CHAIN_EXECUTE=true.
В GUI пользователь нажимает кнопку вручную (торговля не автоматическая).

Алгоритм:
  1. Берём quote-объект (Price) из price_monitor / market_data.
  2. POST /v6/swap (Jupiter API) → получаем transaction в base64.
  3. Десериализуем, подписываем своим Keypair (из wallet.py).
  4. Отправляем через Solana RPC с priority fee.
  5. Ожидаем подтверждение транзакции (polling confirmTransaction).

Ограничения:
  - Только DEX-нога (Raydium/Jupiter). Backpack CEX-ордера не автоматизированы.
  - Jupiter API часто недоступен через прокси → используй осторожно.

Режим DRY_RUN=true (config.DRY_RUN): шаги 3-5 пропускаются, только лог.
ENABLE_CHAIN_EXECUTE=false: executor вообще не вызывается.

Связь с CONTEXT.md: разделы «Планируемые улучшения» и «Файлы».
"""

import asyncio
import base64
from typing import Optional

import httpx
from solders.keypair import Keypair          # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

import config
from logger import log
from price_monitor import Price


async def build_swap_transaction(
    quote: dict,
    user_pubkey: str,
) -> Optional[str]:
    """
    Вызывает Jupiter /v6/swap и возвращает транзакцию в base64.
    quote — это raw_quote из объекта Price (ответ /v6/quote).
    """
    payload = {
        "quoteResponse":           quote,
        "userPublicKey":           user_pubkey,
        "wrapAndUnwrapSol":        True,
        "prioritizationFeeLamports": int(config.PRIORITY_FEE_SOL * 1e9),
        # Динамический priority fee для конкурентного попадания в блок
        "dynamicComputeUnitLimit": True,
    }

    try:
        async with httpx.AsyncClient(**config.httpx_client_kwargs(timeout=15.0)) as client:
            resp = await client.post(
                config.JUPITER_SWAP_URL,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("swapTransaction")
    except httpx.HTTPStatusError as e:
        log.error(f"build_swap_transaction HTTP {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        log.error(f"build_swap_transaction error: {e}")
    return None


async def sign_and_send(
    tx_base64: str,
    keypair: Keypair,
) -> Optional[str]:
    """
    Десериализует транзакцию, подписывает и отправляет в сеть.
    Возвращает signature (txid) или None при ошибке.
    """
    try:
        raw_bytes = base64.b64decode(tx_base64)
        tx = VersionedTransaction.from_bytes(raw_bytes)

        # Подписываем транзакцию
        tx.sign([keypair])

        async with AsyncClient(config.RPC_URL) as rpc:
            opts = TxOpts(
                skip_preflight=False,
                preflight_commitment=Confirmed,
                max_retries=3,
            )
            resp = await rpc.send_raw_transaction(bytes(tx), opts=opts)
            sig = str(resp.value)
            log.info(f"Транзакция отправлена: https://solscan.io/tx/{sig}")
            return sig

    except Exception as e:
        log.error(f"sign_and_send error: {e}")
        return None


async def wait_for_confirmation(sig: str, max_wait_sec: int = 30) -> bool:
    """
    Ждёт подтверждения транзакции.
    Возвращает True если транзакция подтверждена.
    """
    try:
        async with AsyncClient(config.RPC_URL) as rpc:
            for _ in range(max_wait_sec):
                resp = await rpc.get_signature_statuses([sig])
                status = resp.value[0]
                if status is not None:
                    if status.err:
                        log.error(f"Транзакция провалена: {status.err}")
                        return False
                    if status.confirmation_status in ("confirmed", "finalized"):
                        log.info(f"Транзакция подтверждена ({status.confirmation_status})")
                        return True
                await asyncio.sleep(1)
    except Exception as e:
        log.error(f"wait_for_confirmation error: {e}")
    log.warning(f"Транзакция не подтверждена за {max_wait_sec}с")
    return False


def _is_jupiter_swap_quote(raw: dict) -> bool:
    """Только ответ /v6/quote с маршрутом можно скормить /v6/swap."""
    return bool(raw.get("routePlan")) and "inAmount" in raw and "outAmount" in raw


async def execute_swap(
    price: Price,
    keypair: Keypair,
    label: str = "",
) -> bool:
    """
    Полный цикл исполнения одного свопа:
      1. Строим транзакцию через Jupiter /v6/swap
      2. Подписываем и отправляем
      3. Ждём подтверждения

    Возвращает True при успехе.
    """
    if price.source == "backpack" or not _is_jupiter_swap_quote(price.raw_quote):
        log.error(
            f"[{label}] Своп невозможен: это не Jupiter quote (venue={price.source}). "
            "Backpack — только через подписанный REST API биржи."
        )
        return False

    pubkey = str(keypair.pubkey())
    log.info(
        f"[{label}] Своп: {price.input_amount:.4f} {price.input_mint[-6:]} "
        f"→ ~{price.output_amount:.4f} {price.output_mint[-6:]}  "
        f"маршрут: {price.route_label}"
    )

    if config.DRY_RUN:
        log.info(f"[DRY_RUN] Транзакция НЕ отправлена (DRY_RUN=true в .env)")
        return True

    tx_b64 = await build_swap_transaction(price.raw_quote, pubkey)
    if not tx_b64:
        log.error(f"[{label}] Не удалось получить транзакцию от Jupiter")
        return False

    sig = await sign_and_send(tx_b64, keypair)
    if not sig:
        return False

    confirmed = await wait_for_confirmation(sig)
    return confirmed
