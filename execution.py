"""Frozen, expiring manual execution plans. No order submission or fill inference."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR
import math

from strategy import Event, Tick, limit_price


def floor_cent(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def invalid_reason(plan, now, quote, config):
    """Same checks power backend validation, UI timers and recorded replay."""
    if plan.get("status") != "active":
        return plan.get("reason", "提醒已取消")
    if now >= plan["expires_at"]:
        return "20秒执行窗口已到期" if config["entry_valid_seconds"] == 20 else "执行窗口已到期"
    if not quote:
        return "没有可复核的当前行情"
    age = now-quote["batch_ms"]/1000
    if not -5 <= age <= config["entry_max_batch_age_seconds"] or now-quote["ts"] > config["entry_max_batch_age_seconds"]:
        return "行情过旧或时间异常"
    elapsed = quote.get("request_elapsed")
    if elapsed is None or not math.isfinite(elapsed) or not 0 <= elapsed <= config["entry_max_request_seconds"]:
        return "请求耗时超过执行阈值或未记录"
    price = Decimal(str(quote["price"]))
    if price >= Decimal(str(plan["reference_limit"])):
        return "已达到参考涨停价"
    if price > Decimal(str(plan["max_price"])):
        return "价格超过固定委托上限，不追价"
    if price <= Decimal(str(plan["cancel_price"])):
        return "价格跌回突破取消价"
    return ""


@dataclass
class ActivePlan:
    plan: dict
    quote: dict


class Execution:
    def __init__(self, config):
        self.c = config
        self.active, self.latest, self.seen = {}, {}, set()

    def cancel(self, code, reason, now):
        active = self.active.pop(code, None)
        if active is None:
            return None
        recorded = reason == "已录入持仓，不再重复提示买入"
        plan = dict(active.plan, status="recorded" if recorded else "cancelled", reason=reason, cancelled_at=now)
        self.latest[code] = plan
        q = active.quote
        return Event("持仓已记录" if recorded else "买入取消", code, plan["name"], now, q["price"], reason, batch_ms=q["batch_ms"],
                     metrics=dict(execution=dict(plan), quote_received_at=q["ts"]))

    def advance(self, now, positions=None, reason=None):
        events = []
        allocation_reason = ""
        if positions is not None:
            unrecorded = [a for code, a in self.active.items() if code not in positions]
            if len(positions)+len(unrecorded) > self.c["entry_max_positions"]:
                allocation_reason = "本地持仓变化，仓位名额不足，取消未执行提醒"
            occupied = sum((Decimal(str(p["cost"]))*Decimal(str(p["shares"])) for p in positions.values()), Decimal(0))
            reserved = sum((Decimal(str(a.plan["max_notional"]))+Decimal(str(a.plan["fee_reserve"])) for a in unrecorded), Decimal(0))
            if occupied+reserved > Decimal(str(self.c["entry_account_capital"])):
                allocation_reason = "本地资金占用变化，估算资金不足，取消未执行提醒"
        for code, active in list(self.active.items()):
            why = reason or ("已录入持仓，不再重复提示买入" if positions is not None and code in positions else "")
            why = why or allocation_reason or invalid_reason(active.plan, now, active.quote, self.c)
            if why:
                events.append(self.cancel(code, why, now))
        return events

    def observe(self, tick, elapsed, positions):
        if tick.code in self.active:
            self.active[tick.code].quote = dict(**asdict(tick), request_elapsed=elapsed)
        return self.advance(tick.ts, positions)

    def offer(self, signal, tick, elapsed, positions):
        signal_id = f"{tick.code}:{signal.ts:.6f}"
        if signal_id in self.seen:
            return None
        self.seen.add(signal_id)
        if tick.code in self.active:
            return None  # A second structure cannot replace a still-active frozen plan.
        q = dict(**asdict(tick), request_elapsed=elapsed)
        reason = ""
        level = signal.metrics.get("breakout_level")
        if not isinstance(level, (int, float)) or not math.isfinite(level) or level <= 0:
            reason = "未记录有效突破位"
        capital = Decimal(str(self.c["entry_account_capital"]))
        cap = min(floor_cent(Decimal(str(tick.price))*(1+Decimal(str(self.c["entry_slippage_pct"]))/100)),
                  Decimal(str(limit_price(tick.prev)))-Decimal("0.01"))
        cancel = floor_cent(level) if not reason else Decimal("0")
        plan = dict(id=signal_id, code=tick.code, name=tick.name, created_at=tick.ts,
                    expires_at=tick.ts+self.c["entry_valid_seconds"], reference_price=tick.price,
                    max_price=float(cap), cancel_price=float(cancel), breakout_level=level,
                    reference_limit=limit_price(tick.prev), status="active", reason="", quantity=0,
                    max_notional=0, account_capital=float(capital), fee_reserve=self.c["entry_fee_reserve"])
        reason = reason or invalid_reason(plan, tick.ts, q, self.c)
        if cap <= cancel or cap <= 0:
            reason = reason or "突破位与委托上限之间没有有效价格空间"
        if positions is None:
            reason = reason or "缺少完整本地持仓记录，无法检查仓位"
        elif tick.code in positions:
            reason = reason or "已经持有该股，不重复加仓"
        elif len(positions)+len(self.active) >= self.c["entry_max_positions"]:
            reason = reason or "本地持仓及有效提醒已占满仓位名额"
        if not reason:
            occupied = sum((Decimal(str(p["cost"]))*Decimal(str(p["shares"])) for p in positions.values()), Decimal(0))
            reserved = sum((Decimal(str(a.plan["max_notional"]))+Decimal(str(a.plan["fee_reserve"])) for a in self.active.values()), Decimal(0))
            cash = capital-occupied-reserved
            budget = min(capital*Decimal(str(self.c["entry_position_fraction"])), cash)-Decimal(str(self.c["entry_fee_reserve"]))
            qty = max(0, int(budget/(cap*100)))*100
            if qty < 100:
                reason = "按本地资金与费用预留估算，不足买入一手"
            plan.update(quantity=qty, max_notional=float(cap*qty))
        if reason:
            rejected = dict(plan, status="blocked", reason=reason)
            self.latest[tick.code] = rejected
            return Event("执行暂缓", tick.code, tick.name, tick.ts, tick.price, reason, signal.low, tick.batch_ms,
                         dict(execution=dict(rejected)))
        self.active[tick.code] = ActivePlan(dict(plan), q)
        self.latest[tick.code] = dict(plan)
        return Event("买入提醒", tick.code, tick.name, tick.ts, tick.price,
                     "结构确认；手机复核当前价格、实际资金与可成交情况，使用不超过上限的限价单",
                     signal.low, tick.batch_ms, dict(execution=dict(plan), execution_quote=dict(q)))
