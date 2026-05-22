# Backpack ↔ Raydium (Solana)

Spread monitoring between **Backpack Exchange** (CEX, public ticker) and **Raydium** (DEX).
The Raydium quote is fetched **only from Raydium pools** via the Jupiter Quote API (`dexes=Raydium`) — this is not trading "through an aggregator across all DEXes".

## Quick Start

```bash
pip install -r requirements.txt
copy .env.example .env   # Windows
python main.py --monitor
```

### GUI (tabbed window, order book, balances, buttons without auto-trading)

```bash
python gui_app.py
```

- **Monitoring** Tab — Backpack vs Raydium prices, spread, CEX order book, "opportunity", Solana and Backpack balances.
- **Trading** Tab — swap **only by button click** and after confirming `Yes` in the dialog (Raydium via Jupiter). There are no automated trades.

### `[Errno 11001] getaddrinfo failed` Error (Windows)

- **Backpack Order Book 400:** the `limit` parameter for `/api/v1/depth` only accepts `5,10,20,50,100,500,1000`. Any other value returns a 400 error — in the code, the limit is normalized automatically.
- **Jupiter + SOCKS SSL EOF:** try setting **`HTTPX_VERIFY_SSL=false`** in your `.env` (only if you trust the proxy) or use an **`https://`** proxy. The **`socks5h://`** scheme in `.env` is allowed, but **is automatically converted to `socks5://`**, because the combination of `httpx==0.27` + `httpcore` does not understand `socks5h` (otherwise you get an *Unknown scheme* error).

DNS fails to resolve the host (often `quote-api.jup.ag`). Try: a different DNS, VPN, or set **`PROXY_URL`**. You need `pip install -r requirements.txt` which includes `httpx[socks]` for SOCKS5 support. The SOL price might be fetched from CoinGecko, but the Raydium quote won't arrive without access to Jupiter.

- Without `BP_TOKEN_MINT` — demo uses **SOL/USDC**.
- With `BP_TOKEN_MINT` — uses **BP/USDC** (symbol on Backpack: `BACKPACK_SYMBOL_BP_USDC`).

## Where to view the output

- Console (`INFO`)
- `arbi.log` file (`DEBUG`)

## Trade Execution

- **Backpack**: requires a [signed REST API](https://docs.backpack.exchange/) (ED25519), which is not yet implemented in the code.
- **Raydium**: executed via Jupiter `/v6/swap` + wallet signature (see `executor.py`).
- By default, **`ENABLE_CHAIN_EXECUTE=false`** — signals only. If `true`, **only** a partial scenario of "buy base on Raydium" is possible for the Raydium→Backpack direction (selling on CEX must be done manually).
