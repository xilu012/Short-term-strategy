from datetime import date, datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import monitor
from market_context import MarketContext
from strategy import CN


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.today = date(2026, 10, 26)
        self.now = datetime(2026, 10, 26, 10, tzinfo=CN).timestamp()
        self.context = MarketContext(monitor.configuration(), Path(self.tmp.name), self.today)

    def test_calendar_previous_trading_day_skips_weekend(self):
        self.context.accept("calendar", dict(item=[dict(date="20261023"), dict(date="20261026")]), self.now, 0)
        self.assertEqual(datetime.fromtimestamp(self.context.previous_day/1000, CN).date(), date(2026, 10, 23))

    def test_incomplete_previous_pool_is_not_labeled_first_board(self):
        c = self.context
        c.accept("previous", dict(pagination=dict(page=1, pages=2, total=2), item=[dict(thscode="600000.SH", continue_day_cnt=1)]), self.now, 0)
        self.assertEqual(c.view("000001.SZ", self.now)["pattern"], "首板/连板待核实")
        c.accept("previous", dict(pagination=dict(page=2, pages=2, total=2), item=[dict(thscode="600001.SH", continue_day_cnt=2)]), self.now, 1)
        self.assertIn("昨日1连板", c.view("600000.SH", self.now)["pattern"])
        self.assertIn("首板候选", c.view("000001.SZ", self.now)["pattern"])

    def test_wrong_date_market_statistics_rejected(self):
        with self.assertRaises(ValueError):
            self.context.accept("stats", dict(trade_date="2026-10-23"), self.now, 0)
        self.assertIsNone(self.context.view(now=self.now)["market"])

    def test_stale_indices_not_used_and_sector_relatives_are_observational(self):
        c = self.context
        c.accept("catalog", dict(item=[dict(thscode="881101.TI", name="示例行业")]), self.now, 0)
        c.accept("members", dict(item=[dict(thscode="600000.SH")]), self.now, 0, "881101.TI")
        c.accept("indices", dict(timestamp=self.now*1000, item=[dict(thscode="881101.TI", price_change_ratio_pct=2)]), self.now, 0)
        self.assertEqual(c.view("600000.SH", self.now, 7)["sectors"][0]["relative_gain"], 5)
        self.assertIsNone(c.view("600000.SH", self.now+241, 7)["sectors"][0]["gain"])

    def test_permission_failure_disables_only_optional_task(self):
        client = Mock()
        client.get.side_effect = monitor.ApiError("权限不足", code=2003)
        result = self.context.step(client, self.now, 0)
        self.assertIn("error", result)
        self.assertEqual(self.context.next["calendar"], float("inf"))
        self.assertTrue(self.context.due(6))

    def test_rate_limit_propagates_to_shared_backoff(self):
        client = Mock()
        client.get.side_effect = monitor.ApiError("限流", retry=True, code=4001)
        with self.assertRaises(monitor.ApiError):
            self.context.step(client, self.now, 0)
        self.assertFalse(self.context.due(60))

    def test_optional_timeout_is_local_and_has_short_request_deadline(self):
        client = Mock()
        client.get.side_effect = monitor.ApiError("网络连接失败或超时", retry=True)
        result = self.context.step(client, self.now, 0)
        self.assertIn("error", result)
        self.assertEqual(client.get.call_args.kwargs["request_timeout"], 3)
        self.assertGreaterEqual(self.context.next["calendar"], self.context.c["context_interval_seconds"])
        self.assertLess(self.context.next["calendar"], float("inf"))

    def test_optional_timeout_does_not_pause_primary_monitor(self):
        client = Mock()
        client.universe.return_value = {"600000.SH": dict(name="测试股票")}
        def get(path, **kwargs):
            if path != "/api/a-share/prices/snapshot":
                raise monitor.ApiError("网络超时", retry=True)
            return dict(timestamp=int(time.time()*1000), item=[dict(thscode="600000.SH", last_price=10.5, volume=1000, turnover=10500, prev_price=10, open_price=10.2)]), 0.1
        client.get.side_effect = get
        events = []
        with patch.object(monitor, "DATA", Path(self.tmp.name)), patch.object(monitor, "Client", return_value=client), patch.object(monitor, "session_phase", return_value="market"):
            runner = monitor.Monitor(monitor.configuration(), lambda k,v: events.append((k,v)), threading.Event(), key="test-placeholder", max_cycles=2)
            runner.run()
        self.assertEqual(sum(k == "quotes" for k,v in events), 2)
        self.assertNotIn(("lifecycle", "retrying"), events)
        self.assertIn("网络超时", (Path(self.tmp.name)/"connection.jsonl").read_text())
        self.assertNotIn("test-placeholder", (Path(self.tmp.name)/"connection.jsonl").read_text())

    def test_optional_switch_and_cache(self):
        self.context.c["context_enabled"] = False
        self.assertFalse(self.context.due(1000))
        self.assertFalse(self.context.view()["enabled"])
        self.context.c["context_enabled"] = True
        self.context.accept("members", dict(item=[dict(thscode="600000.SH")]), self.now, 0, "881101.TI")
        loaded = MarketContext(self.context.c, Path(self.tmp.name), self.today)
        self.assertEqual(loaded.members["881101.TI"], ["600000.SH"])

    def test_optional_rights_failure_does_not_stop_price_sampling(self):
        client = Mock()
        client.universe.return_value = {"600000.SH": dict(name="测试股票")}
        def get(path, **kwargs):
            if path != "/api/a-share/prices/snapshot":
                raise monitor.ApiError("权限不足", code=2003)
            return dict(timestamp=int(time.time()*1000), item=[dict(thscode="600000.SH", last_price=10.5, volume=1000, turnover=10500, prev_price=10, open_price=10.2)]), 0.1
        client.get.side_effect = get
        events = []
        with patch.object(monitor, "DATA", Path(self.tmp.name)), patch.object(monitor, "Client", return_value=client), patch.object(monitor, "session_phase", return_value="market"):
            runner = monitor.Monitor(monitor.configuration(), lambda k,v: events.append((k,v)), threading.Event(), key="test-placeholder", max_cycles=2)
            runner.run()
        self.assertNotIn("error", [k for k,v in events])
        self.assertEqual(sum(k == "quotes" for k,v in events), 2)
        self.assertIn("/api/a-share/calendar/trading-days", [call.args[0] for call in client.get.call_args_list])


if __name__ == "__main__":
    unittest.main()
