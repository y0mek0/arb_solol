# CONTEXT — Backpack ↔ Multi-DEX Arbitrage Monitor

> **Для ИИ и разработчика:** этот файл — точка восстановления контекста.
> Читай его полностью перед правками. Он актуален и поддерживается вручную.

---

## ИИ: прочитай эти файлы чтобы понять проект полностью

Если ты только что получил этот файл и не знаешь, что здесь происходит —
выполни следующий план чтения. После него ты будешь знать проект как свой.

### Шаг 1 — конфигурация и типы данных (читай первым, всё остальное на них опирается)

| Файл | Зачем читать |
|------|-------------|
| `config.py` | Все 18 торговых пар, mint-адреса токенов, все лимиты (`MIN_DEPTH_USD`, `MAX_SPREAD_PCT`, `MIN_PROFIT_PCT`), URL всех API, параметры прокси |
| `arbitrage_detector.py` | Формула расчёта профита, типы `ArbitrageSignal` и `Direction` — используются везде |

### Шаг 2 — источники данных (понять откуда берутся цены)

| Файл | Зачем читать |
|------|-------------|
| `raydium_api.py` | Как получаем цену с Raydium v3 (основной DEX-источник) |
| `dex_prices.py` | Как получаем цены с Orca/Meteora/Phoenix через DexScreener + TTL-кэш |
| `backpack_private.py` | Как получаем балансы с Backpack Exchange (ED25519 аутентификация) |
| `proxy_monitor.py` | Как трекается здоровье прокси — singleton `monitor` используется в каждом HTTP-модуле |

### Шаг 3 — центральный оркестратор (самый важный модуль логики)

| Файл | Зачем читать |
|------|-------------|
| `market_data.py` | Собирает всё вместе: тикер + стакан + Raydium v3 + DexScreener + effective prices + arbitrage detect. Два входа: `build_snapshot()` (для GUI одной пары) и `scan_all_pairs_light()` (фоновый скан всех 18 пар) |

### Шаг 4 — GUI (только если меняешь интерфейс)

| Файл | Зачем читать |
|------|-------------|
| `gui_app.py` | Весь интерфейс. Читай docstring в начале файла — там схема всех методов и потоков |

### Шаг 5 — вспомогательные (читай только если касаешься)

| Файл | Когда нужен |
|------|------------|
| `wallet.py` | Если меняешь загрузку keypair или on-chain балансы |
| `executor.py` | Если реализуешь автоматическое исполнение свопов |
| `price_monitor.py` | Только для CLI (`main.py`). В GUI не используется |
| `main.py` | CLI-режим, устарел. Не трогать без нужды |
| `logger.py` | Только если меняешь формат логов |

---

### Что НЕ нужно читать сразу

- `.env` / `.env.example` — только если настраиваешь окружение
- `arbi.log` — лог запусков, не код
- `__pycache__/` — игнорировать

---

---

## Что это и зачем

Приложение на Python с графическим интерфейсом (CustomTkinter).

**Цель:** мониторинг расхождений цен одного и того же токена между:
- **Backpack Exchange** — централизованная биржа (CEX), ордербук
- **Raydium / Orca / Meteora / Phoenix** — децентрализованные биржи (DEX) на блокчейне Solana

Когда цена на CEX выше чем на DEX (или наоборот) — возникает арбитражная возможность:
купить дешевле на одной площадке, продать дороже на другой.

**Важно:** приложение НЕ торгует автоматически. Пользователь нажимает кнопки вручную.
Стратегия: каждая сделка должна завершать круг в USDC (не держать волатильный токен).

---

## Запуск

```bash
python gui_app.py          # GUI (основной режим)
python main.py --monitor   # CLI, только лог цен (устаревший режим)
```

Настройка через `.env` (скопировать из `.env.example`):
```
WALLET_PRIVATE_KEY=...     # base58 приватный ключ Solana-кошелька
BACKPACK_API_KEY=...       # API ключ Backpack (для баланса)
BACKPACK_API_SECRET=...    # API секрет Backpack
PROXY_URL=socks5://user:pass@host:port   # прокси (обязательно для РФ)
BP_TOKEN_MINT=...          # mint-адрес токена BP (если доступен)
MIN_PROFIT_PCT=0.5         # минимальный чистый профит для сигнала (%)
MIN_DEPTH_USD=30           # минимальный объём на ближайшем уровне стакана ($)
MAX_SPREAD_PCT=25          # потолок спреда — выше = ошибка данных
TRADE_AMOUNT_USDC=10       # размер условной сделки для расчёта профита
```

---

## Архитектура и поток данных

```
┌─────────────────────────────────────────────────────────────────┐
│                         gui_app.py                              │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │ Пара-selector│   │ Мониторинг   │   │ История сигналов     │ │
│  │ (dropdown)   │   │ вкладка      │   │ (сворачиваемый скан) │ │
│  └──────┬──────┘   └──────┬───────┘   └──────────────────────┘ │
│         │                 │                                      │
│  _schedule_poll()   _apply_snapshot()   _start_background_scanner()
└─────────┼─────────────────┼──────────────────────────────────────┘
          │                 │
          ▼                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                       market_data.py                            │
│                                                                 │
│  build_snapshot()  — полный снимок для активной пары:           │
│    ├─ fetch_backpack_ticker_sync()  → цена Backpack             │
│    ├─ get_raydium_price()           → цена Raydium v3           │
│    ├─ fetch_token_dex_prices()      → цены Orca/Meteora/Phoenix  │
│    ├─ fetch_backpack_depth_sync()   → стакан (bid/ask уровни)   │
│    ├─ effective_bp_prices()         → первый уровень >= $30     │
│    ├─ detect_arbitrage() × N DEX   → лучшая возможность         │
│    ├─ get_on_chain_balances()       → SOL/USDC на кошельке      │
│    └─ get_capital_balances()        → балансы Backpack          │
│                                                                 │
│  scan_all_pairs_light()  — быстрый фоновый скан ВСЕХ пар:       │
│    └─ _scan_one_pair() × 18 пар параллельно (ThreadPoolExecutor)│
│         ├─ Backpack ticker                                      │
│         ├─ Raydium v3 price                                     │
│         ├─ DexScreener (Orca/Meteora/Phoenix)                   │
│         ├─ Backpack depth (limit=20)                            │
│         └─ detect_arbitrage() → LightSignal                     │
└─────────────────────────────────────────────────────────────────┘
          │
          ├─────────────────────────────────────────┐
          ▼                                         ▼
┌──────────────────┐                    ┌──────────────────────┐
│  raydium_api.py  │                    │    dex_prices.py     │
│  Raydium v3 REST │                    │  DexScreener API     │
│  api-v3.raydium  │                    │  Orca/Meteora/Phoenix│
│  .io/pools/info  │                    │  Кэш 30 сек/токен    │
└──────────────────┘                    └──────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────┐
│                  arbitrage_detector.py                       │
│                                                              │
│  detect_arbitrage(bp_price, dex_price) → ArbitrageSignal    │
│    1. Определяет направление (кто дешевле)                   │
│    2. Считает gross_spread_pct                               │
│    3. Санитарная проверка: spread > MAX_SPREAD_PCT → стейл   │
│    4. Вычитает комиссии: Raydium fee + Backpack taker + сеть │
│    5. Возвращает net_profit_usd / net_profit_pct             │
│    6. is_profitable = net_profit_pct >= MIN_PROFIT_PCT       │
└──────────────────────────────────────────────────────────────┘
```

---

## Файлы — что за что отвечает

| Файл | Роль | Ключевые классы/функции |
|------|------|------------------------|
| `gui_app.py` | **Точка входа GUI.** Весь интерфейс. Фоновый сканер. История. | `ArbiApp`, `HistoryEntry`, `_fmt_entry` |
| `config.py` | **Единственный источник конфигурации.** TOKEN_PAIRS, API URL, комиссии, ограничения. | `TOKEN_PAIRS`, `MIN_DEPTH_USD`, `MAX_SPREAD_PCT` |
| `market_data.py` | **Оркестратор данных.** Собирает `MarketSnapshot` для одной пары. Лёгкий скан всех пар. | `build_snapshot()`, `scan_all_pairs_light()`, `LightSignal`, `MarketSnapshot` |
| `arbitrage_detector.py` | **Математика арбитража.** Входит: две цены. Выходит: сигнал с прибылью. | `detect_arbitrage()`, `ArbitrageSignal`, `Direction` |
| `raydium_api.py` | **Raydium v3 REST API.** Поиск пулов, цена токена, цена SOL. | `get_raydium_price()`, `get_sol_price_from_raydium()` |
| `dex_prices.py` | **Многодексовые цены.** DexScreener → Orca/Meteora/Phoenix. TTL-кэш 30с. | `fetch_token_dex_prices()`, `best_sell_price()` |
| `proxy_monitor.py` | **Мониторинг прокси.** Считает OK/fail запросы, latency, переподключения. | `ProxyMonitor`, `monitor` (singleton) |
| `backpack_private.py` | **Backpack auth API.** ED25519-подпись запросов для балансов и ордеров. | `get_capital_balances()` |
| `price_monitor.py` | **Парсеры цен.** `Price` dataclass, парсинг ответов Backpack тикера и Raydium. | `Price`, `price_from_backpack_ticker()`, `raydium_quote_to_price()` |
| `wallet.py` | **Solana кошелёк.** Загрузка keypair из .env, баланс SOL. | `load_keypair()`, `get_sol_balance()` |
| `executor.py` | **Исполнение свопов.** Jupiter v6 транзакции на Raydium. DRY_RUN режим. | `execute_swap()` |
| `logger.py` | Настройка логирования. | `log` |
| `main.py` | CLI режим (устарел, используй `gui_app.py`). Оставлен для отладки. | — |

---

## Торговые пары (18 штук)

Все пары — `TOKEN/USDC`, сравниваются цены Backpack CEX и 4 DEX Solana.

```
Базовые:   SOL, BTC (wBTC), ETH (wETH)
DeFi:      JUP, RAY, JTO, PYTH, W (Wormhole), MSOL, ORCA, DRIFT, HNT, RENDER
Мемкоины:  BONK, WIF, POPCAT
Стейблы:   USDT/USDC
Backpack:  BP/USDC (если BP_TOKEN_MINT задан в .env)
```

Пары и их mint-адреса определены в `config.py → TOKEN_PAIRS`.

---

## Фильтры качества сигналов (критически важно)

Проблема на рынках: фиктивные спреды из-за:
1. **Копеечных ордеров** — tiny bid/ask ($0.01–$2) в начале стакана
2. **Стейл-рынков** — Backpack может показывать устаревшие цены на малоликвидных парах

Решение — двойной фильтр:

### Фильтр 1: MIN_DEPTH_USD (default $30)
Вместо лучшей цены берём **первый уровень стакана с объёмом ≥ $30**.
В стакане такой уровень помечен `►`.
Только эта цена (effective bid/ask) используется для расчёта.

### Фильтр 2: MAX_SPREAD_PCT (default 25%)
Если gross-спред > 25% — это невозможно на реальном ликвидном рынке.
Сигнал блокируется с пометкой `⛔ СТЕЙЛ / ОШИБКА ДАННЫХ`.

---

## Потоки выполнения в GUI

```
Главный поток (Tkinter)
  │
  ├─ _schedule_poll() каждые N сек
  │    └─ ThreadPoolExecutor.submit(build_snapshot)
  │         └─ done() callback → _apply_snapshot() → обновить UI
  │
  ├─ _start_background_scanner() — daemon thread
  │    └─ scan_all_pairs_light() каждые 8 сек
  │         └─ ThreadPoolExecutor (все пары параллельно)
  │              └─ after(0, _on_bg_scan) → _redraw_history()
  │
  └─ _schedule_proxy_refresh() каждые 5 сек
       └─ _prx_monitor.format_panel() → txt_proxy
```

Все HTTP-запросы — в фоновых потоках. В главном потоке только `self.after(0, callback)`.

---

## Сетевые зависимости

| Источник | URL | Через прокси |
|----------|-----|--------------|
| Backpack ticker | `api.backpack.exchange/api/v1/ticker` | Да |
| Backpack depth | `api.backpack.exchange/api/v1/depth` | Да |
| Backpack balances | `api.backpack.exchange/api/v1/capital` | Да (auth) |
| Raydium v3 | `api-v3.raydium.io/pools/info/mint` | Да |
| DexScreener | `api.dexscreener.com/latest/dex/tokens/` | Да |
| Solana RPC | `api.mainnet-beta.solana.com` | Да |
| CoinGecko (fallback) | `api.coingecko.com` | Да |
| Jupiter (fallback) | `quote-api.jup.ag` | Да (часто недоступен через прокси) |

**Прокси обязателен** для запусков из РФ. Формат в `.env`:
```
PROXY_URL=socks5://user:pass@host:port
```

---

## Математика арбитража

```
gross_spread% = |BP_price - DEX_price| / min(BP, DEX) × 100

total_fee% = Raydium_fee (0.25%)
           + Backpack_taker (0.1%)
           + сеть Solana (~0.001 SOL × SOL_price / trade_size)
           + slippage_est (SLIPPAGE_BPS / 100 × 0.5%)

net_profit_usd = trade_usd × (gross_spread% - total_fee%) / 100
is_profitable  = net_profit_pct >= MIN_PROFIT_PCT
```

Подробный пример — кнопка **«Математика»** в шапке приложения.

---

## GUI — структура окна

```
┌─[Пара: OptionMenu] [Математика] [прокси] [статус]─────────────────────┐
│                                                                        │
│  ┌──────────────────────────────────────┐  ┌──────────────────────┐   │
│  │         ВКЛАДКИ (слева)              │  │  ПАНЕЛЬ ИСТОРИИ      │   │
│  │  ┌─Мониторинг─┐  ┌─Торговля─┐       │  │  (справа, 310px)     │   │
│  │  │ Backpack   │  │ USDC-    │       │  │                      │   │
│  │  │ Best DEX   │  │ стратег. │       │  │ [▼ скан] кнопка      │   │
│  │  │ Спред      │  │ описание │       │  │ ▼ Последний скан     │   │
│  │  │ Возможность│  │         │       │  │   (сворачивается)    │   │
│  │  │ DEX prices │  │ [Своп]   │       │  │ История прибыльных   │   │
│  │  │ BIDS ASKS  │  │         │       │  │ (всегда видна)       │   │
│  │  │ Raydium    │  │         │       │  │                      │   │
│  │  │ On-chain   │  │         │       │  │ ── Прокси ──         │   │
│  │  │ Backpack   │  │         │       │  │ Статус/лог запросов  │   │
│  │  │  балансы   │  │         │       │  │                      │   │
│  └──────────────────────────────────────┘  └──────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Частые проблемы и решения

| Проблема | Причина | Решение |
|----------|---------|---------|
| Спред 700%+ | Стейл-рынок Backpack или ошибка DexScreener | Сработает MAX_SPREAD_PCT |
| Phantom spread | Копеечные ордера в стакане | Сработает MIN_DEPTH_USD |
| Raydium: N/A | Нет пула для пары | Нормально, пара пропускается |
| Backpack: 204 | Пара не существует на Backpack | Нормально, пара пропускается |
| SOL баланс 0 | Неверный RPC (Helius без ключа → 401) | Использовать публичный RPC |
| `socks5h` ошибка | httpx не знает схему socks5h | config.py конвертирует в socks5:// |
| `Cannot access free var 'e'` | Python удаляет exception var после except | Сохранить в `err_msg = str(e)` |

---

## Планируемые улучшения (не реализованы)

- [ ] Атомарное исполнение обеих ног (flash arb)
- [ ] Backpack ORDER API (выставление лимитных ордеров через подпись)
- [ ] WebSocket feed вместо REST polling
- [ ] Уведомления Telegram при прибыльном сигнале
- [ ] Автоматический USDC-репатриация после одиночной ноги
