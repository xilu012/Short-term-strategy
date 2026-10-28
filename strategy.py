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
    metrics: dict = field(default_factory=dict)


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
    base: float = 0
    stable_updates: int = 0
    stable_since: float = 0
    pull_volume: float = 0
    pull_seconds: float = 0
    confirm_updates: int = 0
    confirm_since: float = 0
    confirm_level: float = 0
    confirm_amount: float = 0
    failed_breakouts: int = 0
    last_ratio: float = 0


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
            vwap = t.turnover/t.volume if t.volume else None
            metrics = dict(gain=t.gain, distance_pct=(limit_price(t.prev)/t.price-1)*100,
                           vwap=vwap, vwap_deviation_pct=(t.price/vwap-1)*100 if vwap else None,
                           volume_ratio=s.last_ratio, confirmed_updates=s.confirm_updates,
                           stable_updates=s.stable_updates, failed_breakouts=s.failed_breakouts,
                           breakout_level=s.confirm_level)
            events.append(Event(kind, t.code, t.name, t.ts, t.price, reason, s.low, t.batch_ms, metrics))
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
        dt = t.ts-last.ts if last else 0
        rate = max(0, t.volume-last.volume)/dt if last else 0
        amount = max(0, t.turnover-last.turnover) if last else 0
        s.history.append(t)
        while s.history and t.ts-s.history[0].ts > c["rise_window_seconds"]:
            s.history.popleft()
        if s.phase == "signaled":
            if t.price < s.low or t.price >= limit_price(t.prev) or t.ts-s.signal_ts > c["signal_expiry_seconds"] or (t.price/s.signal_price-1)*100 > c["max_signal_drift_pct"]:
                event("失效", "跌破承接低点、已到参考涨停、信号超时或价格偏离，请勿沿用旧提醒")
                s.phase = "cooldown"
            return events
        if s.phase == "cooldown":
            if t.ts >= s.cooldown_until:
                self.states[t.code] = State(history=deque([t]))
            return events
        if t.gain < c["min_gain_pct"] or t.price >= limit_price(t.prev):
            if s.watch_sent:
                event("失效", "低于观察涨幅下限，或已达到参考涨停价，停止沿用本轮入场判断")
            s.phase, s.watch_sent = "search", False
            return events
        if s.phase == "search":
            base = min(s.history, key=lambda x: x.price)
            elapsed = t.ts-base.ts
            rise = (t.price/base.price-1)*100
            if elapsed >= 10 and len(s.history) >= c["min_rise_samples"] and rise >= c["rise_pct"] and rate > 0:
                s.phase, s.peak = "rising", t.price
                s.base = base.price
                s.rise_rate = (t.volume-base.volume)/elapsed
            return events
        if s.phase == "rising":
            s.peak = max(s.peak, t.price)
            drop = (1-t.price/s.peak)*100
            if drop >= c["pullback_min_pct"]:
                s.phase, s.low, s.low_ts = "pullback", t.price, t.ts
                s.pull_rate, s.resistance = rate, t.price
                s.stable_since, s.stable_updates = t.ts, 0
                s.pull_volume, s.pull_seconds = max(0, t.volume-last.volume), dt
            return events
        if s.phase == "pullback":
            if t.price >= s.peak and not s.watch_sent:
                s.phase, s.watch_sent, s.peak = "rising", False, t.price
                return events
            retained = (t.price-s.base)/max(0.0001, s.peak-s.base)
            if (1-t.price/s.peak)*100 > c["pullback_max_pct"] or retained < c["min_rise_retention_ratio"]:
                if s.watch_sent:
                    event("失效", "回落过深，或前段上涨成果保留不足")
                self.states[t.code] = State(history=deque([t]))
                return events
            if t.price < s.low:
                if s.watch_sent:
                    event("失效", "回落低点被跌破，重新观察")
                s.low, s.low_ts, s.resistance, s.watch_sent = t.price, t.ts, t.price, False
                s.stable_since, s.stable_updates = t.ts, 0
                s.confirm_updates = 0
            elif dt <= c["max_confirmation_gap_seconds"] and rate > 0 and amount > 0:
                s.stable_updates += 1
            else:
                s.stable_since, s.stable_updates, s.confirm_updates = t.ts, 0, 0
            stable = (t.ts-s.stable_since >= c["stabilize_seconds"] and
                      s.stable_updates >= c["min_stable_updates"])
            if not s.watch_sent:
                s.pull_volume += max(0, t.volume-last.volume)
                s.pull_seconds += dt
                s.pull_rate = s.pull_volume/max(1, s.pull_seconds)
                if stable and s.pull_rate <= s.rise_rate*c["pullback_volume_ratio"]:
                    s.watch_sent = True
                    s.resistance = max(t.price, s.low)
                    event("观察", "回落量能收缩，连续有效更新中低点暂稳；等待再次放量突破")
            else:
                rebound = (t.price/s.low-1)*100
                s.last_ratio = rate/max(1, s.pull_rate)
                level = s.confirm_level if s.confirm_updates else s.resistance*(1+c["breakout_buffer_pct"]/100)
                if (stable and rebound >= c["rebound_pct"] and
                    t.price > level and t.price >= last.price and
                    s.last_ratio >= c["rebound_volume_ratio"] and dt <= c["max_confirmation_gap_seconds"]):
                    if not s.confirm_updates:
                        s.confirm_since, s.confirm_level, s.confirm_amount = t.ts, level, 0
                    s.confirm_updates += 1
                    s.confirm_amount += amount
                    if (s.confirm_updates >= c["rebound_confirm_updates"] and
                        t.ts-s.confirm_since >= c["rebound_confirm_seconds"] and
                        s.confirm_amount >= c["min_rebound_amount"]):
                        s.phase, s.signal_ts, s.signal_price = "signaled", t.ts, t.price
                        s.cooldown_until = t.ts+c["cooldown_seconds"]
                        event("入场候选", "缩量回落后低点稳住，再次放量突破局部高点并获得连续更新确认")
                else:
                    if s.confirm_updates:
                        s.failed_breakouts += 1
                    s.confirm_updates = 0
                    if s.failed_breakouts >= c["max_failed_breakouts"]:
                        event("失效", "再次上攻连续确认失败，停止本轮观察")
                        s.phase, s.cooldown_until = "cooldown", t.ts+c["cooldown_seconds"]
                    s.resistance = max(s.resistance, t.price)
            return events
        return events


def holding_event(position, t, config):
    """Manual holdings; T+1 reminders only. Mutates the persisted daily high."""
    today = market_time(t.ts).date()
    buy_day = date.fromisoformat(position["buy_date"])
    if buy_day >= today:
        position["status"] = "买入当日 · T+1"
        return None
    if position.get("peak_day") != str(today):
        position["peak_day"], position["peak"] = str(today), t.price
    position["peak"] = max(position["peak"], t.price)
    clock = market_time(t.ts).strftime("%H:%M")
    profit = (t.price/position["cost"]-1)*100
    peak_profit = (position["peak"]/position["cost"]-1)*100
    giveback = (1-t.price/position["peak"])*100
    vwap = t.turnover/t.volume if t.volume else None
    history = position.setdefault("observations", [])
    history[:] = [r for r in history if 0 < t.ts-r[0] <= 120]
    dense = len(history) >= 3 and all(0 < b[0]-a[0] <= config["max_confirmation_gap_seconds"] for a, b in zip(history, history[1:]))
    dense = dense and t.ts-history[-1][0] <= config["max_confirmation_gap_seconds"]
    support = min(r[1] for r in history) if dense else None
    structural_break = bool(support and vwap and t.price < support and t.price < vwap)
    history.append([t.ts, t.price])
    position["status"] = "次日观察"
    kind = "持仓转弱"
    if profit <= -config["sell_loss_pct"]:
        kind, reason = "持仓风险", "亏损达到实验风险阈值，不等待09:35；请复核可卖数量与实际成交条件"
        position["status"] = "风险复核"
    elif t.price >= limit_price(t.prev):
        position["status"] = "参考涨停附近 · 继续观察"
        return None
    elif clock < "09:35":
        return None
    elif peak_profit >= config["sell_min_peak_profit_pct"] and giveback >= config["sell_giveback_pct"]:
        reason = "次日及以后从程序已观察到的当日高点回落，达到实验利润回吐阈值"
        position["status"] = "利润回吐"
    elif structural_break:
        reason = "跌破近期连续采样的支撑区间且位于成交均价下方，请复核走势"
        position["status"] = "承接转弱"
    elif (market_time(t.ts).hour*60+market_time(t.ts).minute >= 9*60+30+config["sell_review_after_minutes"] and
          t.price < t.opening and vwap and t.price < vwap):
        kind, reason = "持仓复核", "开盘观察期限已到，仍低于开盘价和成交均价，请重新评估继续持有依据"
        position["status"] = "到时复核"
    else:
        return None
    alert_times = position.setdefault("alert_times", {})
    if t.ts-alert_times.get(kind, 0) < config["cooldown_seconds"]:
        return None
    alert_times[kind] = t.ts
    return Event(kind, t.code, t.name, t.ts, t.price, reason, batch_ms=t.batch_ms,
                 metrics=dict(profit_pct=profit, peak_profit_pct=peak_profit, giveback_pct=giveback,
                              vwap=vwap, support=support, holding_age_days=(today-buy_day).days))
