from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
import unittest

from dashboard import DashboardModel
from execution import Execution, invalid_reason
from monitor import configuration, show_event
from strategy import CN, Event, Tick


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.c = configuration()
        self.execution = Execution(self.c)
        ts = datetime(2026, 10, 28, 10, tzinfo=CN).timestamp()
        self.t = Tick("600000.SH", "示例", ts, int(ts*1000), 27.26, 100000, 2726000, 25, 25.8)
        self.signal = Event("入场候选", self.t.code, self.t.name, ts, self.t.price, "结构确认", 27.05,
                            self.t.batch_ms, dict(breakout_level=27.20))

    def offer(self, tick=None, positions=None, elapsed=1.1):
        return self.execution.offer(self.signal, tick or self.t, elapsed, {} if positions is None else positions)

    def fresh(self, seconds, **changes):
        return replace(self.t, ts=self.t.ts+seconds, batch_ms=int((self.t.ts+seconds)*1000), **changes)

    def test_agreed_reference_cap_expiry_and_lot_size(self):
        event = self.offer()
        plan = event.metrics["execution"]
        self.assertEqual(event.kind, "买入提醒")
        self.assertEqual(plan["reference_price"], 27.26)
        self.assertEqual(plan["max_price"], 27.31)
        self.assertEqual(plan["cancel_price"], 27.20)
        self.assertEqual(plan["expires_at"], self.t.ts+20)
        self.assertEqual(plan["quantity"], 600)
        self.assertEqual(plan["max_notional"], 16386)
        self.assertIn("最高委托价 ¥27.31", show_event(asdict(event)))

    def test_cap_and_deadline_never_follow_rising_price(self):
        event = self.offer()
        self.assertFalse(self.execution.observe(self.fresh(5, price=27.30), 1, {}))
        plan = self.execution.active[self.t.code].plan
        self.assertEqual(plan["max_price"], 27.31)
        self.assertEqual(plan["expires_at"], self.t.ts+20)
        cancelled = self.execution.observe(self.fresh(6, price=27.32), 1, {})
        self.assertEqual(cancelled[0].kind, "买入取消")
        self.assertIn("上限", cancelled[0].reason)
        self.assertEqual(event.metrics["execution"]["status"], "active")  # Immutable historical snapshot.
        self.assertEqual(event.metrics["execution"]["max_price"], 27.31)

    def test_exact_ceiling_allowed_cancel_boundary_not_allowed(self):
        self.offer()
        self.assertFalse(self.execution.observe(self.fresh(1, price=27.31), 1, {}))
        cancelled = self.execution.observe(self.fresh(2, price=27.20), 1, {})
        self.assertIn("突破", cancelled[0].reason)

    def test_expires_at_twenty_seconds_even_with_new_quotes(self):
        self.offer()
        for seconds in (5, 10, 15, 19):
            self.assertFalse(self.execution.observe(self.fresh(seconds), 1, {}))
        self.assertTrue(self.execution.active)
        self.assertIn("到期", self.execution.advance(self.t.ts+20)[0].reason)
        self.assertFalse(self.execution.active)

    def test_old_data_cancels_without_a_new_quote(self):
        self.offer()
        events = self.execution.advance(self.t.ts+10.01)
        self.assertIn("行情过旧", events[0].reason)

    def test_bad_entry_quality_is_recorded_without_buy_alert(self):
        for tick, elapsed in ((replace(self.t, batch_ms=self.t.batch_ms-11000), 1), (self.t, 3.01)):
            execution = Execution(self.c)
            event = execution.offer(self.signal, tick, elapsed, {})
            self.assertEqual(event.kind, "执行暂缓")
            self.assertFalse(execution.active)

    def test_missing_breakout_and_missing_position_records_block(self):
        self.signal.metrics = {}
        self.assertEqual(self.offer().kind, "执行暂缓")
        self.signal.metrics = dict(breakout_level=27.20)
        event = Execution(self.c).offer(self.signal, self.t, 1, None)
        self.assertIn("持仓", event.reason)

    def test_reference_limit_caps_order_at_one_cent_below(self):
        tick = replace(self.t, price=27.49)
        event = self.offer(tick)
        self.assertEqual(event.metrics["execution"]["max_price"], 27.49)
        cancelled = self.execution.observe(self.fresh(1, price=27.50), 1, {})
        self.assertIn("参考涨停", cancelled[0].reason)

    def test_duplicate_structure_does_not_send_another_alert(self):
        self.offer()
        self.assertIsNone(self.offer())
        self.assertIsNone(self.execution.offer(replace(self.signal, ts=self.signal.ts+1), self.fresh(1), 1, {}))
        self.assertEqual(self.execution.latest[self.t.code]["status"], "active")
        self.assertEqual(self.execution.latest[self.t.code]["max_price"], 27.31)
        self.execution.advance(self.t.ts+20)
        self.assertIsNone(self.offer())

    def test_recorded_holding_cancels_and_prevents_addition(self):
        self.offer()
        positions = {self.t.code: dict(cost=27, shares=100)}
        recorded = self.execution.advance(self.t.ts+1, positions)[0]
        self.assertIn("持仓", recorded.reason)
        self.assertEqual(recorded.kind, "持仓已记录")
        event = Execution(self.c).offer(self.signal, self.t, 1, positions)
        self.assertEqual(event.kind, "执行暂缓")

    def test_unrelated_new_holdings_revalidate_cash_and_slots(self):
        self.offer()
        positions = {"600001.SH": dict(cost=49, shares=1000)}
        events = self.execution.advance(self.t.ts+1, positions)
        self.assertIn("资金不足", events[0].reason)
        execution = Execution(self.c)
        execution.offer(self.signal, self.t, 1, {})
        positions = {"600001.SH": dict(cost=10, shares=100), "600002.SH": dict(cost=10, shares=100)}
        self.assertIn("名额", execution.advance(self.t.ts+1, positions)[0].reason)

    def test_two_slots_include_pending_reminders(self):
        self.offer()
        other = replace(self.t, code="600001.SH")
        second = self.execution.offer(replace(self.signal, code=other.code), other, 1, {})
        self.assertEqual(second.kind, "买入提醒")
        third = replace(self.t, code="600002.SH")
        event = self.execution.offer(replace(self.signal, code=third.code), third, 1, {})
        self.assertEqual(event.kind, "执行暂缓")
        self.assertIn("名额", event.reason)

    def test_fees_and_account_budget_limit_quantity(self):
        execution = Execution(self.c | {"entry_account_capital": 100, "entry_fee_reserve": 50})
        self.assertEqual(execution.offer(self.signal, self.t, 1, {}).kind, "执行暂缓")
        event = self.offer(positions={"600001.SH": dict(cost=49, shares=900)})
        plan = event.metrics["execution"]
        self.assertEqual(plan["quantity"] % 100, 0)
        self.assertLessEqual(Decimal(str(plan["max_notional"]))+50, Decimal(50000-49*900))

    def test_connection_failure_is_terminal_for_existing_plan(self):
        self.offer()
        events = self.execution.advance(self.t.ts+1, reason="连接重试")
        self.assertEqual(events[0].reason, "连接重试")
        self.assertFalse(self.execution.observe(self.fresh(2), 1, {}))
        self.assertFalse(self.execution.active)

    def test_ui_counts_current_and_cumulative_separately(self):
        event = self.offer()
        model = DashboardModel(self.c)
        model.ingest("lifecycle", "running")
        model.ingest("event", asdict(event))
        self.assertEqual(model.active_count(self.t.ts), 1)
        self.assertEqual(model.counts["买入提醒"], 1)
        self.assertEqual(model.stage(model.quotes[self.t.code], self.t.ts), "买入提醒")
        model.ingest("event", asdict(self.execution.advance(self.t.ts+20)[0]))
        self.assertEqual(model.active_count(self.t.ts+20), 0)
        self.assertEqual(model.counts["买入提醒"], 1)


if __name__ == "__main__":
    unittest.main()
