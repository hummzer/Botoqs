# Gold CLI v3 — Self-Evolving Multi-Strategy SMC + Volume Profile + Absorption Bot

## Setup
```bash
python3 -m gold_cli setup          # Interactive wizard: broker → credentials → strategies → pairs → risk
pip3 install --break-system-packages -r requirements.txt
```

## Commands
```bash
python3 -m gold_cli scan           # Full scan: 5 pairs × 6 timeframes × 3 strategies
python3 -m gold_cli watch -i 60   # Real-time watcher (polls every 60s)
python3 -m gold_cli trades         # Trade history + technique stats + strategy performance
python3 -m gold_cli brokers        # Configured brokers
python3 -m gold_cli config         # Full configuration + technique weights
python3 -m gold_cli pine -o file.pine  # Export PineScript v5 for TradingView/trader dev
python3 -m gold_cli traderdev      # TraderDev: persisted strategy registry
python3 -m gold_cli sharpe         # Performance dashboard: Sharpe, win rates, drawdowns
python3 -m gold_cli mt5-connect   # Connect to MT5 and show account info
```

## Pairs (hardcoded — no others)
- XAUUSD (Gold)
- USOIL (Crude Oil)
- NASDAQ (US100)
- BTCUSD (Bitcoin)
- GBPJPY (Cable Yen)

## 3 Concurrent Strategies

| Strategy | Timeframes | Risk | SL Tightness | R:R Target | Min Confluence | Max Concurrent |
|----------|-----------|------|-------------|------------|----------------|-----------------|
| **smc_core** (intraday) | 15M, 1H, 4H | 10¢/$10 | 1.0× | 2.0 | 4 | 2 |
| **scalp** | 1M, 5M | 5¢/$5 | 0.7× (tight) | 2.0 | 3 | 3 |
| **swing** | 4H, 1D | 25¢/$25 | 1.2× (wide) | 3.0 | 5 | 1 |

Additional strategies available: `breakout`, `liquidity`, `intraday`.

## Core Strategy: Absorption + Volume Profile
- **Buy at VAL** (Value Area Low) — discount zone, buyers absorbing sellers
- **Sell at VAH** (Value Area High) — premium zone, sellers absorbing buyers
- Volume Profile POC/VAH/VAL computed from OHLCV data
- Absorption detection: high-volume reversals that fail to break structure

## Advanced SMC Techniques (11 total)
1. **Market Structure** — BOS + CHoCH detection
2. **Volume Profile** — VAL/VAH buy/sell zones (PRIMARY)
3. **Absorption** — buyer/seller absorption with strength scoring
4. **FVG** — Fair Value Gaps (3-candle imbalance)
5. **iFVG** — Invalidated FVGs (filled gaps → support/resistance)
6. **Invalid OB** — Broken order blocks → reversal zones
7. **Order Blocks** — Valid bullish/bearish OBs with strength
8. **Liquidity Sweep** — Stop hunts that reverse (trap)
9. **Liquidity Run** — Genuine breakouts with follow-through
10. **Failed Auction** — Price rejected at level, reverses
11. **Breakout** — Volume-confirmed breakouts

## Self-Evolving System
- Tracks per-technique stats: trades, wins, losses, win rate, avg R:R, Sharpe, max drawdown, consecutive wins/losses
- Technique weights auto-adjust: good performers increase weight, poor performers decrease
- SL tightness morphs: tighter after 3+ consecutive wins, looser after 3+ consecutive losses
- "Never backwards" — only evolves in direction of improving Sharpe

## TraderDev Persistence
Every strategy configuration + live performance stats saved to `~/.gold_cli/traderdev_strategies.json`.

## Risk Model
- Cent-based or dollar-based accounts
- Slippage + spread buffer + commission modeled before every entry
- Minimum R:R gating after all costs (default 1.5)
- Daily loss limit (default 50¢/$50)
- Trailing stops: breakeven at 1R, then progressive trail

## Performance Targets
- Positive Sharpe ratio
- Consistent R:R (avg win > avg loss)
- Maximum consecutive wins > maximum consecutive losses
- Minimum drawdown
- Weekly targets: 100-1000¢/week (cents) or $100-$500/2 weeks (dollars)

## Data Source
Yahoo Finance: GC=F, CL=F, NQ=F, BTC-USD, GBPJPY=X
Multi-timeframe: 1M, 5M, 15M, 1H, 4H, 1D

## News Filter
Forex Factory calendar — high-impact events filtered by pair relevance.

## Broker Support (Top 10)
justmarkets, oanda, icmarkets, pepperstone, xm, fbs, exness, forex_com, alpari, octa
MT5 + REST API adapters with symbol mapping per broker.

## Rules: When to Use What

### Use Order Blocks when:
- Clear HTF bias aligns with OB direction
- OB has high strength score (>10)
- Price approaching OB from premium (for buys) or discount (for sells)
- Absorption confirmed at OB zone

### Use FVGs when:
- Fresh FVG in direction of trend
- Price retracing into FVG from the wrong side
- FVG aligns with higher TF structure

### Use iFVGs / Invalid OBs when:
- Price has already violated the level (filled FVG / broken OB)
- Price is returning to retest the now-invalidated zone
- HTF bias supports the reversal

### Use Liquidity Sweeps when:
- Clear wick beyond known high/low
- Quick reversal back inside the level
- Absorption at the sweep point

### Use Liquidity Runs when:
- Clean close beyond level with volume
- Follow-through candles in breakout direction
- HTF supports continuation

### Use Failed Auctions when:
- Price attempts to break level, fails, and reverses
- Reversal candle shows clear rejection (long wick)
- HTF structure supports the reversal

### Use Breakouts when:
- Volume-confirmed close beyond consolidation
- Follow-through in breakout direction
- HTF trend aligns

### DON'T use OBs/FVGs when:
- Market is choppy/ranging (ADX < 15)
- No clear HTF structure bias
- Multiple conflicting FVGs/OBs at same level
- News event imminent (reduced confidence)

## Example: Grow 100¢ to 10,000¢
1. Start with `smc_core` + `scalp` strategies
2. Risk 5-10¢ per trade with min 1.5 R:R after costs
3. Target 2-3 trades per day during London/NY sessions
4. Compound wins — increase risk slightly after 5+ consecutive wins
5. Cut risk on 3+ consecutive losses (self-evolve tightens SL)
6. Weekly target: 200-500¢ growth per week → 1000¢ in 2-4 weeks
7. Scale to dollar account at $100+ balance
