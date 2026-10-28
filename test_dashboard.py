from dataclasses import asdict
from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import monitor
from dashboard import Dashboard, DashboardModel
from execution import Execution
from strategy import Engine


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.c = monitor.configuration()
        self.model = DashboardModel(self.c)
        self.model.clear("demo")

    def row(self):
        t = list(monitor.demo_ticks())[0]
        return dict(**asdict(t), phase="signaled", ready=True, position=None)

    def test_mode_switch_clears_demo_quotes_events(self):
        self.model.ingest("quotes", [self.row()])
        self.model.ingest("event", dict(code="600000.SH", kind="观察", ts=1))
        self.model.clear("live")
        self.assertFalse(self.model.quotes)
        self.assertFalse(self.model.events)

    def test_candidate_expiration_and_stale_quote(self):
        row = self.row()
        self.model.clear("live")
        self.model.ingest("event", dict(code=row["code"], kind="入场候选", ts=row["ts"]-31))
        self.assertEqual(self.model.stage(row, row["ts"]), "已过期")
        self.assertEqual(self.model.stage(row, row["ts"]+31), "待更新")

    def test_filter_and_error_state(self):
        self.model.ingest("quotes", [self.row()])
        self.assertEqual(len(self.model.rows("结构确认")), 1)
        self.assertFalse(self.model.rows(search="不存在"))
        self.model.ingest("error", "权限不足")
        self.model.ingest("done", "已停止")
        self.assertEqual(self.model.state, "error")
        self.assertEqual(self.model.status, "权限不足")
        self.assertEqual(self.model.last_error, "权限不足")

    def test_latest_connection_reason_is_retained(self):
        issue = dict(stage="重点股行情", message="HTTP 429", wait=4, time="10:00:00", blocking=True)
        self.model.ingest("connection", issue)
        self.model.ingest("status", "已恢复")
        self.assertEqual(self.model.last_connection, issue)

    def test_paused_monitor_does_not_present_actionable_candidate(self):
        row = self.row()
        self.model.clear("live")
        for state in ("paused", "retrying", "stopping", "stopped", "error"):
            self.model.ingest("lifecycle", state)
            self.assertEqual(self.model.stage(row, row["ts"]), "暂停确认")


@unittest.skipUnless(os.environ.get("DISPLAY"), "需要桌面显示")
class DesktopTests(unittest.TestCase):
    def test_buy_popup_shows_frozen_prices_and_expires_without_new_backend_event(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DATA", Path(directory)):
            root = tk.Tk()
            app = Dashboard(root, monitor.configuration(), monitor)
            try:
                config = monitor.configuration()
                engine, execution = Engine(config), Execution(config)
                reminder = None
                for t in list(monitor.demo_ticks())[:15]:
                    for event in engine.feed(t):
                        if event.kind == "入场候选":
                            reminder = execution.offer(event, t, 0, {})
                value = asdict(reminder)
                app.model.clear("demo")
                app.model.ingest("event", value)
                app.model.ingest("lifecycle", "demo")
                app.notify(value)
                app.refresh(); root.update()
                self.assertEqual(app.cards["有效买入"].get(), "1")
                self.assertIn("最高委托价 ¥10.61", app.popups[reminder.code][2].get())
                self.assertIn("最高委托 10.61", app.execution_text.get())
                quote = dict(value["metrics"]["execution_quote"], phase="signaled", position=None)
                quote.update(ts=quote["ts"]+21, batch_ms=quote["batch_ms"]+21000)
                app.model.ingest("quotes", [quote])
                app.refresh(); root.update()
                self.assertFalse(app.popups)
                self.assertEqual(app.cards["有效买入"].get(), "0")
                self.assertEqual(app.cards["累计提醒"].get(), "1")
                self.assertIn("已取消", app.execution_text.get())
            finally:
                app.close()

    def test_footer_and_error_details_remain_visible_in_small_window(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DATA", Path(directory)):
            root = tk.Tk()
            root.tk.call("tk", "scaling", 2.0)
            app = Dashboard(root, monitor.configuration(), monitor)
            try:
                root.geometry("1080x700")
                app.model.ingest("error", "主行情请求失败，保留原因")
                app.model.ingest("done", "监控已停止")
                app.refresh(); root.update()
                self.assertTrue(app.status_bar.winfo_ismapped())
                self.assertLessEqual(app.status_bar.winfo_y()+app.status_bar.winfo_height(), root.winfo_height())
                self.assertEqual(app.status.get(), "主行情请求失败，保留原因")
                self.assertEqual(str(app.state_label.cget("foreground")), "#ff767d")
                with patch("tkinter.messagebox.showinfo") as show:
                    app.connection_details()
                self.assertIn("主行情请求失败", show.call_args.args[1])
            finally:
                app.close()

    def test_demo_updates_table_chart_alerts_and_clears_on_restart(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DATA", Path(directory)):
            root = tk.Tk()
            errors = []
            root.report_callback_exception = lambda *args: errors.append(args)
            app = Dashboard(root, monitor.configuration(), monitor)
            try:
                app.start("demo")
                deadline = time.monotonic()+12
                while (app.worker.is_alive() or not app.messages.empty()) and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                root.update()
                app.refresh()
                self.assertFalse(app.worker.is_alive())
                self.assertEqual(root.title(), "A股短线雷达")
                self.assertEqual(app.model.counts["入场候选"], 1)
                self.assertEqual(len(app.table.get_children()), 1)
                self.assertGreater(len(app.chart.find_all()), 10)
                self.assertEqual(app.model.counts["买入提醒"], 1)
                self.assertEqual(app.cards["累计提醒"].get(), "1")
                self.assertEqual(app.cards["有效买入"].get(), "0")
                self.assertEqual(len(app.alerts.get_children()), 5)
                self.assertFalse(errors)
                app.model.clear("live")
                app.refresh()
                self.assertFalse(app.table.get_children())
                self.assertFalse(app.alerts.get_children())
            finally:
                app.close()
