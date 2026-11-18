"""Two-filter momentum bot for the cbBTC 24h race (agent-fun/bars-replay/v1).

Strategy, in one paragraph: BTC's 1-minute on-chain price is mostly noise
(about 7 bps of standard deviation per minute) with its money made in
multi-hour drifts, while a full-size round trip costs about 26-30 bps in
fees, slippage and price impact. So this bot only buys when the drift is
unmistakable on two horizons at once -- the 60-minute return above +50 bps
AND the 120-minute return above +30 bps -- and then rides it with a slow
exit: it sells when the 60-minute return turns below -50 bps, when the
price falls 90 bps from the peak since entry, or on a hard stop 80 bps
under the entry close. It never trades during warm-up, never uses more
than the cash it holds, and holds at most one position, all-in.

A late safety net: if no trade has happened by bar 1200 (the last four
hours), a single 60-minute return above +35 bps is enough to enter, so
the bot does not sit out a whole quiet window and miss the guardrail of
at least one trade. On the dev window this net never fires.

Everything is integer math on closes; no clock, no network, no files,
no randomness, so two runs on the same data produce identical replies.
"""

import json
import re
import sys

# Minimal inline transport for agent-fun/bars-replay/v1 (RULES.txt, "Protocol
# and runtime"): NDJSON on stdin/stdout. The harness sends hello once, then
# one batch per minute, then done; the bot answers every batch with exactly
# one reply echoing its seq, flushed before the next line is read. Only the
# state the strategy reads is kept: cash, positions, seq and warm-up.
# Everything else the full SDK shim offers (depths, impact, weights) is not
# used here and is left out on purpose.

PROTOCOL = "agent-fun/bars-replay/v1"
MAX_HARNESS_LINE_BYTES = 1_048_576
MAX_BOT_LINE_BYTES = 65_536
U64_MAX = 2**64 - 1

_U64_RE = re.compile(r"0|[1-9][0-9]{0,19}")
_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,20}")
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9-][A-Za-z0-9_-]{0,63}")
_ORDER_KEYS = {
    "buy": ("side", "client_id", "symbol", "notional_quote_units", "max_slippage_bps"),
    "sell": ("side", "client_id", "symbol", "qty", "max_slippage_bps"),
}


class ProtocolError(Exception):
    """The harness sent something this bot cannot follow."""


class OrderError(Exception):
    """The strategy returned something the harness would reject as a violation."""


class _Holding:
    """A held position: quantity at the symbol's qty_scale, and its cost."""

    __slots__ = ("qty", "cost")

    def __init__(self, qty, cost):
        self.qty = qty
        self.cost = cost


class _State:
    """The bot's view of the replay, current before each on_batch call."""

    def __init__(self, hello):
        self.hello = hello
        self.seq = -1
        self.t = -1
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = int(hello["capital"]["quote_units"])
        self.positions = {}
        self.warmup_bars = hello.get("warmup_bars", 0)
        self._used = set()
        self._next = 1

    @property
    def warming_up(self):
        """True while the batch being answered is a warm-up batch: any order decided on it is rejected."""
        return self.warmup_bars > 0 and self.seq < self.warmup_bars

    def _apply_account(self, account):
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {
            p["symbol"]: _Holding(int(p["qty"]), int(p["cost_quote_units"]))
            for p in account["positions"]
        }

    def _to_orders(self, result):
        """What on_batch returned as wire orders (missing client_ids assigned).

        Raises OrderError on a violation or more orders than the hello allows.
        """
        if result is None:
            return []
        if not isinstance(result, (list, tuple)):
            raise OrderError("on_batch must return a list of orders")
        cap = self.hello["caps"]["max_orders_per_bar"]
        if len(result) > cap:
            raise OrderError(
                f"on_batch returned {len(result)} orders, over max_orders_per_bar ({cap})"
            )
        checked = [_check_order(o) for o in result]
        taken = {o["client_id"] for o in checked if o["client_id"] is not None}
        orders = []
        for o in checked:
            cid = o["client_id"]
            if cid is None:
                cid = self._fresh_id(taken)
            taken.add(cid)
            amount_key = _ORDER_KEYS[o["side"]][3]
            orders.append(
                {
                    "side": o["side"],
                    "client_id": cid,
                    "symbol": o["symbol"],
                    amount_key: o["amount"],
                    "max_slippage_bps": o["bps"],
                }
            )
        return orders

    def _record_orders(self, orders):
        for o in orders:
            self._used.add(o["client_id"])

    def _fresh_id(self, taken):
        while True:
            cid = f"o{self._next}"
            self._next += 1
            if cid not in self._used and cid not in taken:
                return cid


def _to_u64(value):
    """An amount (int or u64 decimal string) as a canonical u64 decimal string."""
    n = None
    if type(value) is int:
        n = value
    elif isinstance(value, str) and _U64_RE.fullmatch(value):
        n = int(value)
    if n is None or n < 0 or n > U64_MAX:
        raise OrderError(f"amount must be a u64 (int or decimal string), got {value!r}")
    return str(n)


def _check_order(value):
    """One order as the harness expects it, or OrderError."""
    if not isinstance(value, dict):
        raise OrderError(f"expected an order dict, got {type(value).__name__}")
    side = value.get("side")
    if side not in ("buy", "sell"):
        raise OrderError(f'side must be "buy" or "sell", got {side!r}')
    keys = _ORDER_KEYS[side]
    for k in value:
        if k not in keys:
            raise OrderError(f"unknown key {k!r} on a {side} order")
    symbol = value.get("symbol")
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise OrderError(f"symbol must be a ticker of 1-20 [A-Z0-9], got {symbol!r}")
    amount = _to_u64(value.get(keys[3]))
    bps = value.get("max_slippage_bps")
    if type(bps) is not int or not 0 <= bps <= 10_000:
        raise OrderError(f"max_slippage_bps must be an int 0..10000, got {bps!r}")
    cid = value.get("client_id")
    if cid is not None and (not isinstance(cid, str) or not _CLIENT_ID_RE.fullmatch(cid)):
        raise OrderError(
            "client_id must be 1-64 of [A-Za-z0-9_-] not starting with _"
        )
    return {"side": side, "client_id": cid, "symbol": symbol, "amount": amount, "bps": bps}


def _read_lines(src):
    """Yield the lines of a binary stream as UTF-8 (newline-terminated, a trailing CR dropped)."""
    line_no = 0
    while True:
        raw = src.readline(MAX_HARNESS_LINE_BYTES + 1)
        if not raw:
            return
        line_no += 1
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        elif len(raw) > MAX_HARNESS_LINE_BYTES:
            raise ProtocolError(f"input line {line_no} is over {MAX_HARNESS_LINE_BYTES} bytes")
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        try:
            yield raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError(f"input line {line_no} is not valid UTF-8") from None


def _run(on_batch):
    """Speak agent-fun/bars-replay/v1 until done, then stop reading."""
    src = sys.stdin.buffer
    out = sys.stdout
    state = None
    line_no = 0
    for line in _read_lines(src):
        line_no += 1
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            raise ProtocolError(f"input line {line_no} is not JSON") from None
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            if state is not None:
                raise ProtocolError(f"input line {line_no}: a second hello")
            if msg.get("protocol") != PROTOCOL:
                raise ProtocolError(
                    f"the harness speaks {msg.get('protocol')!r}; this bot speaks {PROTOCOL}"
                )
            state = _State(msg)
        elif kind in ("batch", "done"):
            if state is None:
                raise ProtocolError(f"input line {line_no}: {kind} before hello")
            seq = msg.get("seq")
            account = msg.get("account")
            shaped = (
                type(seq) is int
                and seq == state.seq + 1
                and isinstance(account, dict)
                and isinstance(account.get("positions"), list)
            )
            if kind == "done":
                if not shaped:
                    raise ProtocolError(
                        f"input line {line_no}: malformed done (seq {seq!r} after batch {state.seq})"
                    )
                state.seq = msg["seq"]
                state._apply_account(msg["account"])
                return
            if not (
                shaped
                and type(msg.get("t")) is int
                and isinstance(msg.get("bars"), list)
                and isinstance(msg.get("fills"), list)
            ):
                raise ProtocolError(
                    f"input line {line_no}: malformed batch (seq {seq!r} after {state.seq})"
                )
            state.seq = msg["seq"]
            state.t = msg["t"]
            state._apply_account(msg["account"])
            orders = state._to_orders(on_batch(state, msg))
            text = json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if len(text.encode("utf-8")) > MAX_BOT_LINE_BYTES:
                raise OrderError(
                    f"batch {msg['seq']}: the reply is over {MAX_BOT_LINE_BYTES} bytes"
                )
            state._record_orders(orders)
            out.write(text + "\n")
            out.flush()
        else:
            raise ProtocolError(f"input line {line_no}: unknown message type {kind!r}")
    if state is None:
        raise ProtocolError("input ended before hello")
    raise ProtocolError(f"input ended before done (after {state.seq + 1} batches)")

SYMBOL = "CBBTC"

# Entry: 60-minute return above this (bps) AND 120-minute return above this.
ENTRY_60_BPS = 50
ENTRY_120_BPS = 30
# Exit: 60-minute return below this (bps), or a pullback from the peak since
# entry of this (bps), or a fall under the entry close of this (bps).
EXIT_60_BPS = -50
TRAIL_BPS = 90
STOP_BPS = 80
# Late safety net: from this batch on, a 60-minute return above this alone
# is enough to enter (only if nothing has traded yet).
LATE_BAR = 1200
LATE_ENTRY_60_BPS = 35
# Cap an order's move from the latest close; 1-minute moves are ~7 bps, so
# 300 bps only ever binds on a wild gap, where refusing is the safe side.
MAX_SLIPPAGE_BPS = 300

_closes = []
_entry_close = None
_peak_close = None
_traded = False


def _ret_bps(now, back):
    """Return of `now` over `back` in basis points (integer, floored)."""
    return (now - back) * 10000 // back


def on_batch(state, batch):
    global _entry_close, _peak_close, _traded
    if state.warming_up:
        return []
    bar = None
    for b in batch["bars"]:
        if b["symbol"] == SYMBOL:
            bar = b
            break
    if bar is None:
        return []
    close = int(bar["c"])
    _closes.append(close)
    holding = state.positions.get(SYMBOL)
    qty = holding.qty if holding is not None else 0

    if qty > 0:
        if close > _peak_close:
            _peak_close = close
        pullback = (_peak_close - close) * 10000 // _peak_close
        ret60 = _ret_bps(close, _closes[-61]) if len(_closes) > 60 else 0
        if (
            pullback >= TRAIL_BPS
            or ret60 < EXIT_60_BPS
            or close * 10000 <= _entry_close * (10000 - STOP_BPS)
        ):
            _entry_close = None
            _peak_close = None
            return [
                {
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            ]
        return []

    n = len(_closes)
    if n > 120:
        ret60 = _ret_bps(close, _closes[-61])
        ret120 = _ret_bps(close, _closes[-121])
        enter = ret60 > ENTRY_60_BPS and ret120 > ENTRY_120_BPS
        if not enter and not _traded and state.seq >= LATE_BAR and ret60 > LATE_ENTRY_60_BPS:
            enter = True
        if enter and state.cash > 0:
            _entry_close = close
            _peak_close = close
            _traded = True
            return [
                {
                    "side": "buy",
                    "symbol": SYMBOL,
                    "notional_quote_units": state.cash,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            ]
    return []


_run(on_batch)
