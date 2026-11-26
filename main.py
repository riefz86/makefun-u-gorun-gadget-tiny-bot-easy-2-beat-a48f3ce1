#!/usr/bin/env python3
"""Tide-Rider: a small trend-following paper-trading bot for Solana hourly bars.

This bot plays the agent-fun/bars-replay/v1 line protocol: the harness sends one
JSON line at a time on stdin (``hello``, then ``batch`` after ``batch``, then a
final ``done``), and this program answers every batch with exactly one JSON
reply line on stdout carrying the orders it decided on that bar.  Nothing is
read from the clock, the environment, the network or any file, and nothing is
written to disk, so two runs over the same bars produce byte-identical action
logs.  Diagnostics go to stderr only; stdout carries replies and nothing else.

Why trend-following beats buy-and-hold here
--------------------------------------------
Buy-and-hold owns the whole universe through every drawdown of the window.
A 3-day window of hourly Solana prices is short enough that one sharp leg down
can erase the window for a holder, yet long enough that a trend filter can sit
most of it out in cash.  This bot therefore only owns an asset while its short
trend points up, and steps aside the moment the trend turns, the asset gets
violently choppy, or a trailing stop is hit.  It never shorts, never uses
leverage, and never bets more than a quarter of equity on one symbol, so the
worst case is a slow bleed of small fees, not a blow-up.

The rules, per symbol, in plain words
--------------------------------------
1.  Keep a fast exponential average (6 hours) and a slow one (18 hours) of the
    hourly closes, plus the last 24 hourly log returns for a volatility read.
2.  Go long only when the fast average sits clearly above the slow one (a gap
    of at least 0.3%, so flat noise does not trigger a trade), the last 6 hours
    actually rose, and hourly volatility is ordinary (between 0.2% and 6%):
    dead-flat markets whipsaw a crossover, and a volatility explosion means
    the tape is moving on news rather than trend.
3.  Size by risk, not by hope: a calm asset gets a bigger slice than a jumpy
    one.  The target is 14% of equity scaled by 2% / hourly-volatility,
    clamped to 7%-21% of equity, and always under the harness weight cap.
4.  Leave when the trend leaves: fast average crossing back under the slow
    one, a 6% fall from the best close since entry (trailing stop), a 10% fall
    from the entry close (hard stop), or hourly volatility jumping past 8%.
5.  A portfolio guard watches total equity: a 10% drawdown from the running
    peak flattens everything and pauses new entries for 12 bars, so one bad
    stretch cannot compound into a guardrail breach (the contest rejects any
    entry whose maximum drawdown passes 25%).
6.  After any exit, wait 2 bars before re-entering the same symbol, so a
    single wobble cannot buy-sell-buy the same hour and burn fees.

Money and protocol details
--------------------------
* Capital is USDC with 6 decimals; every amount in an order is an integer
  number of micro-USDC.  Prices arrive as integer strings at each symbol's own
  price scale, quantities at its qty scale; the value of a holding is
  ``qty * price * 10**quote_scale // 10**(qty_scale + price_scale)``.
* A buy states the notional it may spend; a sell states the exact quantity to
  sell, taken from the account the harness reports, never from a guess.
* Orders decided on bar ``k`` fill at bar ``k + 1``'s open (latency 1), so the
  decision always uses closes that are already known, never the bar being
  filled.  Orders decided during a warm-up are rejected by the harness, so
  this bot sends none while warming up.  The last bar's orders are rejected
  too, and every position is flattened at the end, so the bot stops opening
  new positions on the final batch.
* ``max_slippage_bps`` is 100 (1%): wide enough for an ordinary hourly open,
  tight enough that a gap open does not fill at a price the trend never saw.
"""

import json
import math
import re
import sys

PROTOCOL = "agent-fun/bars-replay/v1"

# --- Strategy constants (all in plain sight, so a reader can audit them). ---
FAST_LEN = 6            # fast EMA length, in hourly bars
SLOW_LEN = 18           # slow EMA length, in hourly bars
VOL_WINDOW = 24         # hourly returns kept for the volatility read
MOMENTUM_BARS = 6       # bars looked back for the entry momentum check
ENTRY_GAP = 0.003       # fast must beat slow by this much to enter (0.3%)
EXIT_GAP = 0.001        # fast below slow by this much exits (0.1% hysteresis)
MIN_VOL = 0.002         # hourly volatility below this: too flat, skip entries
MAX_VOL_ENTRY = 0.06    # hourly volatility above this: too wild, skip entries
MAX_VOL_EXIT = 0.08     # hourly volatility above this: leave the position
BASE_WEIGHT = 0.14      # target equity weight before the volatility scaling
MIN_WEIGHT = 0.07       # floor of the volatility-scaled weight
MAX_WEIGHT = 0.21       # ceiling of the volatility-scaled weight
REF_VOL = 0.02          # volatility the base weight is calibrated to (2%/hour)
TRAILING_STOP = 0.06    # exit after a 6% fall from the best close since entry
HARD_STOP = 0.10        # exit after a 10% fall from the entry close
REENTRY_COOLDOWN = 2    # bars to wait after an exit before re-entering a symbol
PORTFOLIO_DD = 0.10     # flatten everything after a 10% equity drawdown
PORTFOLIO_COOLDOWN = 12  # bars to pause entries after a portfolio flatten
MAX_SLIPPAGE_BPS = 100  # slippage bound on every order (1%)
BPS = 10_000

_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,20}")
_U64_RE = re.compile(r"0|[1-9][0-9]{0,19}")


def _log(msg):
    """One diagnostic line on stderr; stdout stays pure protocol."""
    print(msg, file=sys.stderr, flush=True)


class SymbolState:
    """The per-symbol memory: EMA pair, return history, and trade bookkeeping."""

    __slots__ = ("ema_fast", "ema_slow", "closes", "returns", "entry_price",
                 "peak_price", "cooldown_until", "last_raw")

    def __init__(self):
        self.ema_fast = None
        self.ema_slow = None
        self.closes = []        # recent closes as floats, oldest first
        self.returns = []       # recent hourly log returns, oldest first
        self.entry_price = None
        self.peak_price = None
        self.cooldown_until = -1
        self.last_raw = None    # latest close as the raw integer the harness sent

    def observe(self, close):
        """Fold one hourly close into the averages and the return history."""
        if self.closes:
            prev = self.closes[-1]
            if prev > 0 and close > 0:
                self.returns.append(math.log(close / prev))
                if len(self.returns) > VOL_WINDOW:
                    self.returns.pop(0)
        self.closes.append(close)
        if len(self.closes) > VOL_WINDOW + 2:
            self.closes.pop(0)
        alpha_fast = 2.0 / (FAST_LEN + 1)
        alpha_slow = 2.0 / (SLOW_LEN + 1)
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
        else:
            self.ema_fast += alpha_fast * (close - self.ema_fast)
            self.ema_slow += alpha_slow * (close - self.ema_slow)

    @property
    def ready(self):
        """True once both averages have seen a full slow window of closes."""
        return self.ema_fast is not None and len(self.closes) >= SLOW_LEN

    def volatility(self):
        """Standard deviation of the recent hourly log returns, or None."""
        n = len(self.returns)
        if n < 8:
            return None
        mean = sum(self.returns) / n
        var = sum((r - mean) ** 2 for r in self.returns) / n
        return math.sqrt(var) if var > 0 else 0.0

    def momentum(self):
        """Log return over the last MOMENTUM_BARS closes, or None."""
        if len(self.closes) < MOMENTUM_BARS + 1:
            return None
        first = self.closes[-MOMENTUM_BARS - 1]
        last = self.closes[-1]
        if first <= 0 or last <= 0:
            return None
        return math.log(last / first)


class Bot:
    """The whole strategy: protocol state plus one SymbolState per asset."""

    def __init__(self, hello):
        self.hello = hello
        self.symbols = {}
        for entry in hello["symbols"]:
            self.symbols[entry["symbol"]] = {
                "price_scale": int(entry["price_scale"]),
                "qty_scale": int(entry["qty_scale"]),
            }
        self.quote_scale = int(hello["capital"]["quote_scale"])
        self.capital = int(hello["capital"]["quote_units"])
        self.caps = hello["caps"]
        self.fees = hello["fees"]
        self.warmup_bars = int(hello.get("warmup_bars", 0))
        self.state = {sym: SymbolState() for sym in self.symbols}
        self.seq = -1
        self.cash = self.capital
        self.equity = self.capital
        self.positions = {}     # symbol -> qty (int, at the symbol's qty scale)
        self.peak_equity = self.capital
        self.portfolio_cooldown_until = -1
        self._next_order = 1

    # -- account and market bookkeeping ------------------------------------
    def apply_account(self, account):
        """Trust the harness account over any local guess of cash and holdings."""
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {}
        for pos in account.get("positions", []):
            qty = int(pos["qty"])
            if qty > 0:
                self.positions[pos["symbol"]] = qty
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

    def close_of(self, bar, symbol):
        """A bar's close as a float in whole-asset units (dollars, not raw)."""
        scale = self.symbols[symbol]["price_scale"]
        return int(bar["c"]) / (10 ** scale)

    def value_of(self, symbol, qty, raw_price):
        """What ``qty`` of a symbol is worth at the raw integer ``raw_price``,
        in micro-USDC -- exact integer arithmetic, no float round-trip, so a
        weight-room check can never overshoot the harness cap by a rounding
        error."""
        info = self.symbols[symbol]
        return (qty * raw_price * (10 ** self.quote_scale)
                // (10 ** (info["qty_scale"] + info["price_scale"])))

    # -- order construction -------------------------------------------------
    def _fresh_client_id(self):
        cid = "o%d" % self._next_order
        self._next_order += 1
        return cid

    def _buy(self, symbol, notional):
        """A validated buy order dict, or None when the amount is unusable."""
        if not _SYMBOL_RE.fullmatch(symbol):
            return None
        if type(notional) is not int or notional <= 0:
            return None
        if notional < int(self.fees["min_notional_quote_units"]):
            return None
        return {"side": "buy", "client_id": self._fresh_client_id(),
                "symbol": symbol, "notional_quote_units": str(notional),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _sell(self, symbol, qty):
        """A validated sell order dict for a whole position, or None."""
        if not _SYMBOL_RE.fullmatch(symbol):
            return None
        if type(qty) is not int or qty <= 0:
            return None
        return {"side": "sell", "client_id": self._fresh_client_id(),
                "symbol": symbol, "qty": str(qty),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _target_notional(self, symbol, vol):
        """Risk-scaled buy size in micro-USDC for one symbol.

        Entries only happen while flat in the symbol, so the weight room is
        simply the harness cap times equity; the target is the smaller of the
        volatility-scaled weight, that room, and the cash on hand.
        """
        scale = REF_VOL / vol if vol and vol > 0 else 1.0
        weight = BASE_WEIGHT * scale
        weight = max(MIN_WEIGHT, min(MAX_WEIGHT, weight))
        room = self.caps["max_weight_bps"] * self.equity // BPS
        target = int(self.equity * weight)
        return min(target, room, self.cash)

    # -- the decision itself -------------------------------------------------
    def decide(self, batch):
        """Return the orders for one batch (a list, possibly empty)."""
        self.seq = int(batch["seq"])
        self.apply_account(batch["account"])
        for bar in batch.get("bars", []):
            symbol = bar.get("symbol")
            if symbol not in self.state:
                continue
            try:
                st = self.state[symbol]
                st.last_raw = int(bar["c"])
                st.observe(self.close_of(bar, symbol))
            except (KeyError, TypeError, ValueError):
                _log("batch %d: skipping malformed bar for %s" % (self.seq, symbol))

        if self.seq < self.warmup_bars:
            return []  # warm-up bars: every order would be rejected, so send none
        return self._trade()

    def _trade(self):
        orders = []
        # Portfolio guard first: a deep equity drawdown flattens everything.
        if self.equity < self.peak_equity * (1.0 - PORTFOLIO_DD):
            for symbol, qty in sorted(self.positions.items()):
                order = self._sell(symbol, qty)
                if order is not None:
                    orders.append(order)
                    self.state[symbol].cooldown_until = self.seq + PORTFOLIO_COOLDOWN
            if orders:
                self.portfolio_cooldown_until = self.seq + PORTFOLIO_COOLDOWN
                self.peak_equity = self.equity  # re-anchor: do not re-trigger at once
                _log("batch %d: portfolio guard flattened %d positions"
                     % (self.seq, len(orders)))
            return orders

        for symbol in sorted(self.state):
            st = self.state[symbol]
            if not st.ready:
                continue
            close = st.closes[-1]
            held = self.positions.get(symbol, 0)
            vol = st.volatility()
            if vol is None:
                continue
            try:
                if held > 0:
                    order = self._maybe_exit(symbol, st, close, vol, held)
                elif (self.seq >= self.portfolio_cooldown_until
                        and self.seq >= st.cooldown_until):
                    order = self._maybe_enter(symbol, st, close, vol)
                else:
                    order = None
            except (TypeError, ValueError, ZeroDivisionError):
                _log("batch %d: skipping %s on bad arithmetic" % (self.seq, symbol))
                continue
            if order is not None:
                orders.append(order)
            if len(orders) >= int(self.caps["max_orders_per_bar"]):
                break
        return orders

    def _maybe_exit(self, symbol, st, close, vol, held):
        """Sell a holding when its trend, stop or volatility says leave."""
        fast, slow = st.ema_fast, st.ema_slow
        if st.peak_price is None or close > st.peak_price:
            st.peak_price = close
        reasons = []
        if fast < slow * (1.0 - EXIT_GAP):
            reasons.append("trend turned")
        if st.entry_price and close < st.entry_price * (1.0 - HARD_STOP):
            reasons.append("hard stop")
        if st.peak_price and close < st.peak_price * (1.0 - TRAILING_STOP):
            reasons.append("trailing stop")
        if vol > MAX_VOL_EXIT:
            reasons.append("volatility blowout")
        if not reasons:
            return None
        st.cooldown_until = self.seq + REENTRY_COOLDOWN
        st.entry_price = None
        st.peak_price = None
        _log("batch %d: exit %s (%s) at %.6g" % (self.seq, symbol, ", ".join(reasons), close))
        return self._sell(symbol, held)

    def _maybe_enter(self, symbol, st, close, vol):
        """Buy when trend, momentum and volatility all agree, sized by risk."""
        fast, slow = st.ema_fast, st.ema_slow
        if fast < slow * (1.0 + ENTRY_GAP):
            return None
        mom = st.momentum()
        if mom is None or mom <= 0:
            return None
        if vol < MIN_VOL or vol > MAX_VOL_ENTRY:
            return None
        if len(self.positions) >= int(self.caps["max_open_positions"]):
            return None
        notional = self._target_notional(symbol, vol)
        if notional < int(self.fees["min_notional_quote_units"]):
            return None
        st.entry_price = close
        st.peak_price = close
        _log("batch %d: enter %s notional %d (fast %.6g slow %.6g vol %.4f)"
             % (self.seq, symbol, notional, fast, slow, vol))
        return self._buy(symbol, notional)


def _reply(seq, orders):
    """The one reply line for a batch, byte for byte in protocol shape."""
    return json.dumps({"type": "reply", "seq": seq, "orders": orders},
                      separators=(",", ":"), ensure_ascii=False)


def main():
    """Speak bars-replay/v1 on stdin/stdout until the harness says done.

    An empty stdin (no hello at all, as when the program is run bare for a
    smoke test) is not a session: exit 0 quietly instead of failing a check
    that never started.  A stream that ends mid-session, or carries a second
    hello, is a broken session and exits 1.
    """
    src = sys.stdin.buffer
    out = sys.stdout
    bot = None
    saw_hello = False
    while True:
        raw = src.readline(1_048_576 + 1)
        if not raw:
            if not saw_hello:
                return 0
            _log("input ended mid-session after batch %d" % (bot.seq if bot else -1))
            return 1
        line = raw.decode("utf-8", errors="strict").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _log("input line is not JSON: %r" % line[:80])
            return 1
        if not isinstance(msg, dict):
            _log("input line is not an object")
            return 1
        kind = msg.get("type")
        if kind == "hello":
            if saw_hello:
                _log("second hello")
                return 1
            if msg.get("protocol") != PROTOCOL:
                _log("unknown protocol: %r" % (msg.get("protocol"),))
                return 1
            try:
                bot = Bot(msg)
            except (KeyError, TypeError, ValueError) as exc:
                _log("malformed hello: %s" % exc)
                return 1
            saw_hello = True
        elif kind == "batch":
            if bot is None:
                _log("batch before hello")
                return 1
            try:
                orders = bot.decide(msg)
            except (KeyError, TypeError, ValueError) as exc:
                _log("batch %s failed: %s" % (msg.get("seq"), exc))
                return 1
            text = _reply(int(msg["seq"]), orders)
            if len(text.encode("utf-8")) > 65_536:
                _log("reply over 64 KiB at batch %s" % msg.get("seq"))
                return 1
            out.write(text + "\n")
            out.flush()
        elif kind == "done":
            if bot is None:
                _log("done before hello")
                return 1
            _log("done at seq %s: final equity %d micro-USDC"
                 % (msg.get("seq"), bot.equity))
            return 0
        else:
            _log("unknown message type: %r" % (kind,))
            return 1


if __name__ == "__main__":
    sys.exit(main())
