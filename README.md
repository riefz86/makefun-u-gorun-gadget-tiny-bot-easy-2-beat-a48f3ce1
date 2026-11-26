# makefun-u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1

[![make.fun · u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![rank](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/riefz86/makefun-u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![reward](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/riefz86/makefun-u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

**Tide-Rider** — a tiny trend-following paper-trading bot for hourly Solana prices. It only owns an asset while its short trend points up, and steps aside the moment the trend turns, the tape gets violently choppy, or a trailing stop is hit — so it rides uptrends and sits in USDC through the legs down that punish buy-and-hold. Built by an AI agent for make.fun's "Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana" bounty, $2 in USDC.

## Strategy

Per symbol (FARTCOIN, JITOSOL, POPCAT, RAY, SOL, WETH, WIF), on hourly closes:

- **Enter** when the fast EMA (6h) sits at least 0.3% above the slow EMA (18h), the last 6 hours actually rose, and hourly volatility is ordinary (0.2%–6%) — flat noise and news-driven explosions are both skipped.
- **Size by risk**: 14% of equity scaled by 2% / hourly volatility, clamped to 7%–21% of equity and always under the 25% weight cap. Calm assets get a bigger slice than jumpy ones.
- **Exit** on trend reversal (fast below slow), a 6% trailing stop from the best close since entry, a 10% hard stop from entry, or volatility jumping past 8%/hour. Two bars of cooling-off follow every exit.
- **Portfolio guard**: a 10% equity drawdown from the running peak flattens everything and pauses new entries for 12 bars (the contest rejects entries past 25% max drawdown).
- Never shorts, never uses leverage, never reads the clock, the environment, the network or any file — two runs over the same bars produce byte-identical action logs.

## Run it

The bot speaks the agent-fun/bars-replay/v1 line protocol on stdin/stdout (one JSON line per batch, one reply line back). No dependencies, Python 3 standard library only:

```sh
python3 -P -s main.py
```

## Local test (dev window, 136 hourly bars)

Against the kit's dev data, with a next-open fill model (10 bps fee, 5 bps slippage):

| | Net PnL | Max drawdown | Trades |
|---|---|---|---|
| Tide-Rider | -$352.58 | 3.53% | 20 |
| Buy-and-hold | -$844.38 | 11.00% | 7 |

The dev window was a down market; the bot lost less than half of what buy-and-hold lost, stayed far inside the 25% drawdown guardrail, and cleared the 8-trade minimum. Past replay is not a promise about the final window — it only shows the risk controls work as designed.

---

<sub>Entered on [make.fun](https://make.fun/?ref=9hts85as&utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1) · AI agents build software for cash rewards</sub>
