"""Experimental, causal state machine. Percent values are percentage points."""
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
import re

CN = timezone(timedelta(hours=8))


def market_time(ts):
    return datetime.fromtimestamp(ts, CN)


def main_board(meta, today=None):
    today = today or datetime.now(CN).date()
    code, name = meta.get("thscode", ""), meta.get("name", "")
    if not re.fullmatch(r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ)", code):
        return False
    if not name or any(x in name.upper() for x in ("ST", "退")):
        return False
    try:
        listed = date.fromisoformat(meta["list_date"])
        ended = meta.get("end_date")
        return (today - listed).days >= 60 and (not ended or date.fromisoformat(ended) > today)
    except (KeyError, TypeError, ValueError):
        return False  # Unknown listing status is excluded, not guessed.


def limit_price(prev):
    # Reference only: no authoritative per-stock limit-price field in this snapshot API.
    return float((Decimal(str(prev)) * Decimal("1.1")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class Tick:
    code: str
    name: str
    ts: float  # Local reception time; API only exposes a batch-level market timestamp.
    batch_ms: int
    price: float
    volume: float
    turnover: float
    prev: float
    opening: float

    @property
    def gain(self):
        return (self.price / self.prev - 1) * 100

    @classmethod
    def parse(cls, row, ts, batch_ms, name):
        vals = [float(row[k]) for k in ("last_price", "volume", "turnover", "prev_price", "open_price")]
        if not all(math.isfinite(x) for x in vals) or min(vals[0], vals[3], vals[4]) <= 0 or min(vals[1:3]) < 0:
            raise ValueError("Invalid quote")
        return cls(row["thscode"], name, ts, int(batch_ms), *vals)


@dataclass
class Event:
    kind: str
    code: str
    name: str
    ts: float
    price: float
    reason: str
    low: float = 0
    batch_ms: int = 0


@dataclass
class State:
    history: deque = field(default_factory=deque)
    phase: str = "search"
    peak: float = 0
    low: float = 0
    low_ts: float = 0
    rise_rate: float = 0
    pull_rate: float = 0
    resistance: float = 0
    watch_sent: bool = False
    cooldown_until: float = 0
    signal_price: float = 0
    signal_ts: float = 0


class Engine:
    def __init__(self, config):
        self.c = config
        self.states = {}
        self.day = None

    def feed(self, t):
        c = self.c
        day = market_time(t.ts).date()
        if day != self.day:
            self.states.clear()
            self.day = day
        s = self.states.setdefault(t.code, State())
        events = []
        def event(kind, reason):
            events.append(Event(kind, t.code, t.name, t.ts, t.price, reason, s.low, t.batch_ms))
        if s.history:
            last = s.history[-1]
            if t.ts <= last.ts:
                return events
            if t.ts-last.ts > c["max_observation_gap_seconds"] or t.volume < last.volume or t.turnover < last.turnover:
                if s.phase == "signaled":
                    event("失效", "采样中断或累计量重置，原候选失效")
                s = self.states[t.code] = State()
            elif (t.price, t.volume, t.turnover) == (last.price, last.volume, last.turnover):
                # Do not use unchanged/cached observations to confirm stabilization.
                if s.phase == "signaled" and t.ts-s.signal_ts > c["signal_expiry_seconds"]:
                    event("失效", "信号已超时，缓存或无成交行情不能延长有效期")
                    s.phase = "cooldown"
                return events
        last = s.history[-1] if s.history else None
        rate = max(0, t.volume-last.volume)/(t.ts-last.ts) if last else 0
        s.history.append(t)
        while s.history and t.ts-s.history[0].ts > c["rise_window_seconds"]:
            s.history.popleft()
        if s.phase == "signaled":
            if t.price < s.low or t.ts-s.signal_ts > c["signal_expiry_seconds"] or (t.price/s.signal_price-1)*100 > c["max_signal_drift_pct"]:
                event("失效", "跌破承接低点、信号超时或价格已偏离触发位置，请勿沿用旧提醒")
                s.phase = "cooldown"
            return events
        if s.phase == "cooldown":
            if t.ts >= s.cooldown_until:
                self.states[t.code] = State(history=deque([t]))
            return events
        if not c["min_gain_pct"] <= t.gain <= c["max_gain_pct"] or t.price >= limit_price(t.prev):
            if s.watch_sent:
                event("失效", "已离开实验买入涨幅区间，或接近参考涨停价")
            s.phase, s.watch_sent = "search", False
            return events
        if s.phase == "search":
            base = min(s.history, key=lambda x: x.price)
            elapsed = t.ts-base.ts
            rise = (t.price/base.price-1)*100
            if elapsed >= 10 and rise >= c["rise_pct"] and rate > 0:
                s.phase, s.peak = "rising", t.price
                s.rise_rate = (t.volume-base.volume)/elapsed
            return events
        if s.phase == "rising":
            s.peak = max(s.peak, t.price)
            drop = (1-t.price/s.peak)*100
            if drop >= c["pullback_min_pct"]:
                s.phase, s.low, s.low_ts = "pullback", t.price, t.ts
                s.pull_rate, s.resistance = rate, t.price
            return events
        if s.phase == "pullback":
            if t.price >= s.peak and not s.watch_sent:
                if s.watch_sent:
                    event("失效", "已突破原拉升高点，本轮候选结束，等待新结构")
                s.phase, s.watch_sent, s.peak = "rising", False, t.price
                return events
            if (1-t.price/s.peak)*100 > c["pullback_max_pct"]:
                if s.watch_sent:
                    event("失效", "回落幅度超出实验承接范围")
                self.states[t.code] = State(history=deque([t]))
                return events
            if t.price < s.low:
                if s.watch_sent:
                    event("失效", "回落低点被跌破，重新观察")
                s.low, s.low_ts, s.resistance, s.watch_sent = t.price, t.ts, t.price, False
            stable = t.ts-s.low_ts >= c["stabilize_seconds"]
            if not s.watch_sent:
                s.pull_rate = s.pull_rate*0.7 + rate*0.3
                if stable and s.pull_rate <= s.rise_rate*c["pullback_volume_ratio"]:
                    s.watch_sent = True
                    s.resistance = max(t.price, s.low)
                    event("观察", "拉升后回落，成交增量收缩、低点暂稳；仅观察，尚未触发入场候选")
            else:
                rebound = (t.price/s.low-1)*100
                if (stable and rebound >= c["rebound_pct"] and
                    t.price > s.resistance*(1+c["breakout_buffer_pct"]/100) and
                    rate >= max(1, s.pull_rate)*c["rebound_volume_ratio"]):
                    s.phase, s.signal_ts, s.signal_price = "signaled", t.ts, t.price
                    s.cooldown_until = t.ts+c["cooldown_seconds"]
                    event("入场候选", "回落低点暂稳后，成交增量放大并突破观察期局部高点；请手机确认，不自动下单")
                else:
                    s.resistance = max(s.resistance, t.price)
            return events
        return events


def holding_event(position, t, config):
    """Manual holdings; T+1 reminders only. Mutates the persisted daily high."""
    today = market_time(t.ts).date()
    if date.fromisoformat(position["buy_date"]) >= today:
        return None
    if position.get("peak_day") != str(today):
        position["peak_day"], position["peak"] = str(today), t.price
    position["peak"] = max(position["peak"], t.price)
    clock = market_time(t.ts).strftime("%H:%M")
    if clock < "09:35" or t.price >= limit_price(t.prev):
        return None
    profit = (t.price/position["cost"]-1)*100
    peak_profit = (position["peak"]/position["cost"]-1)*100
    giveback = (1-t.price/position["peak"])*100
    if profit <= -config["sell_loss_pct"]:
        reason = "次日及以后亏损达到实验退出阈值；请确认可成交情况"
    elif peak_profit >= config["sell_min_peak_profit_pct"] and giveback >= config["sell_giveback_pct"]:
        reason = "次日及以后从程序已观察到的当日高点回落，达到实验利润回吐阈值"
    else:
        return None
    if t.ts-position.get("last_alert", 0) < config["cooldown_seconds"]:
        return None
    position["last_alert"] = t.ts
    return Event("持仓转弱", t.code, t.name, t.ts, t.price, reason, batch_ms=t.batch_ms)
