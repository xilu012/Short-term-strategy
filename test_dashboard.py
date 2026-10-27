from dataclasses import asdict
from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import monitor
from dashboard import Dashboard, DashboardModel


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
        self.assertEqual(len(self.model.rows("入场候选")), 1)
        self.assertFalse(self.model.rows(search="不存在"))
        self.model.ingest("error", "权限不足")
        self.model.ingest("done", "已停止")
        self.assertEqual(self.model.state, "error")

    def test_paused_monitor_does_not_present_actionable_candidate(self):
        row = self.row()
        self.model.clear("live")
        for state in ("paused", "retrying", "stopping", "stopped", "error"):
            self.model.ingest("lifecycle", state)
            self.assertEqual(self.model.stage(row, row["ts"]), "暂停确认")


@unittest.skipUnless(os.environ.get("DISPLAY"), "需要桌面显示")
class DesktopTests(unittest.TestCase):
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
                self.assertEqual(len(app.alerts.get_children()), 3)
                self.assertFalse(errors)
                app.model.clear("live")
                app.refresh()
                self.assertFalse(app.table.get_children())
                self.assertFalse(app.alerts.get_children())
            finally:
                app.close()
