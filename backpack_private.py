"""
backpack_private.py — аутентифицированные запросы к Backpack Exchange API.

Используется только для получения балансов (USDC, SOL, BP, ...) на CEX.
НЕ используется для выставления ордеров (не реализовано).

Механизм аутентификации Backpack:
  1. Строим строку = "instruction=balanceQuery&timestamp=...&window=..."
  2. Подписываем ED25519-ключом (BACKPACK_API_SECRET — Base64-seed, 32 байта)
  3. Кладём в заголовки: X-API-Key, X-Signature, X-Timestamp, X-Window

Требует в .env:
  BACKPACK_API_KEY    — Base64 публичного ключа (как отображает интерфейс биржи)
  BACKPACK_API_SECRET — Base64 seed приватного ключа (32 байта после decode)

Публичные функции:
  get_capital_balances() → dict[str, float]
      Возвращает {ticker: доступный_баланс} только для ненулевых позиций.
      Вызывается из market_data.build_snapshot() → отображается в GUI (балансы Backpack).

Связь с CONTEXT.md: раздел «Файлы — что за что отвечает».
"""

from __future__ import annotations

import base64
import time
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config


def _sign(instruction: str, params: Optional[dict[str, str]], window_ms: int = 5000) -> tuple[dict[str, str], int]:
    """Возвращает заголовки X-* и timestamp ms."""
    ts = int(time.time() * 1000)
    parts: list[str] = [f"instruction={instruction}"]
    if params:
        for k in sorted(params.keys()):
            parts.append(f"{k}={params[k]}")
    parts.append(f"timestamp={ts}")
    parts.append(f"window={window_ms}")
    signing_str = "&".join(parts)

    secret_raw = base64.b64decode(config.BACKPACK_API_SECRET)
    sk = Ed25519PrivateKey.from_private_bytes(secret_raw)
    sig = sk.sign(signing_str.encode("utf-8"))
    sig_b64 = base64.b64encode(sig).decode("ascii")

    headers = {
        "X-Timestamp": str(ts),
        "X-Window": str(window_ms),
        "X-API-Key": config.BACKPACK_API_KEY,
        "X-Signature": sig_b64,
    }
    return headers, ts


def get_capital_balances() -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """
    GET /api/v1/capital — балансы по активам на Backpack.
    Возвращает (json_dict, error_message).
    """
    if not config.BACKPACK_API_KEY or not config.BACKPACK_API_SECRET:
        return None, "Нет BACKPACK_API_KEY / BACKPACK_API_SECRET в .env"

    base = config.BACKPACK_API_BASE.rstrip("/")
    url = f"{base}/api/v1/capital"
    headers, _ = _sign("balanceQuery", None)

    kw = config.httpx_client_kwargs(timeout=20.0)

    try:
        with httpx.Client(**kw) as client:
            r = client.get(url, headers=headers)
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            return r.json(), None
    except Exception as e:
        return None, str(e)
