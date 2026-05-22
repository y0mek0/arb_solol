# Backpack ↔ Raydium (Solana)

Мониторинг спреда между **Backpack Exchange** (CEX, публичный тикер) и **Raydium** (DEX).  
Котировка Raydium берётся **только с пулов Raydium** через Jupiter Quote API (`dexes=Raydium`) — это не торговля «через агрегатор по всем DEX».

## Запуск

```bash
pip install -r requirements.txt
copy .env.example .env   # Windows
python main.py --monitor
```

### GUI (окно с вкладками, стакан, балансы, кнопки без авто-торговли)

```bash
python gui_app.py
```

- Вкладка **Мониторинг** — цены Backpack vs Raydium, спред, стакан CEX, «возможность», балансы Solana и Backpack.
- Вкладка **Торговля** — своп **только по кнопке** и после `Да` в диалоге (Raydium через Jupiter). Авто-сделок нет.

### Ошибка `[Errno 11001] getaddrinfo failed` (Windows)

- **Стакан Backpack 400:** параметр `limit` у `/api/v1/depth` только `5,10,20,50,100,500,1000`. Любое другое значение даёт 400 — в коде лимит нормализуется автоматически.
- **Jupiter + SOCKS SSL EOF:** попробуй **`HTTPX_VERIFY_SSL=false`** в `.env` (только если доверяешь прокси) или прокси **`https://`**. Схема **`socks5h://`** в `.env` допустима, но **автоматически превращается в `socks5://`**, т.к. связка `httpx==0.27` + `httpcore` не понимает `socks5h` (иначе ошибка *Unknown scheme*).

DNS не резолвит хост (часто `quote-api.jup.ag`). Попробуй: другой DNS, VPN, или **`PROXY_URL`**. Нужен `pip install -r requirements.txt` с `httpx[socks]` для SOCKS5. Цена SOL может подтянуться с CoinGecko, но котировка Raydium без доступа к Jupiter не придёт.

- Без `BP_TOKEN_MINT` — демо **SOL/USDC**.
- С `BP_TOKEN_MINT` — **BP/USDC** (символ на Backpack: `BACKPACK_SYMBOL_BP_USDC`).

## Где смотреть вывод

- Консоль (`INFO`)
- Файл `arbi.log` (`DEBUG`)

## Исполнение сделок

- **Backpack**: нужен [подписанный REST API](https://docs.backpack.exchange/) (ED25519), в коде пока не реализовано.
- **Raydium**: через Jupiter `/v6/swap` + подпись кошельком (см. `executor.py`).
- По умолчанию **`ENABLE_CHAIN_EXECUTE=false`** — только сигналы. При `true` возможен **только** частичный сценарий «купить base на Raydium» при направлении Raydium→Backpack (продажа на CEX вручную).
