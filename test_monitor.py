import contextlib
from dataclasses import asdict, replace
from datetime import date, datetime
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

import monitor
from strategy import CN, Engine, Tick, holding_event, main_board


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.c = monitor.configuration()
        self.ticks = list(monitor.demo_ticks())

    def test_pullback_recovery_and_invalidation(self):
        engine = Engine(self.c)
        events = [e for t in self.ticks for e in engine.feed(t)]
        self.assertEqual([e.kind for e in events], ["观察", "入场候选", "失效"])
        self.assertLess(events[0].ts, events[1].ts)
        self.assertEqual(events[1].low, 10.5)

    def test_straight_rise_is_not_a_buy(self):
        engine = Engine(self.c)
        events = [e for t in self.ticks[:5] for e in engine.feed(t)]
        self.assertFalse(events)

    def test_no_rebound_volume_no_buy(self):
        engine = Engine(self.c)
        events = []
        volume, turnover = 100000, 1000000
        for i, t in enumerate(self.ticks[:-1]):
            delta = 10000 if i < 5 else 100
            volume += delta
            turnover += delta*t.price
            events.extend(engine.feed(replace(t, volume=volume, turnover=turnover)))
        self.assertNotIn("入场候选", [e.kind for e in events])

    def test_observation_gap_discards_old_setup(self):
        engine = Engine(self.c)
        for t in self.ticks[:13]:
            engine.feed(t)
        t = replace(self.ticks[13], ts=self.ticks[13].ts+100)
        self.assertFalse(engine.feed(t))
        self.assertEqual(engine.states[t.code].phase, "search")

    def test_cached_quote_expires_and_cannot_confirm_stability(self):
        engine = Engine(self.c)
        for t in self.ticks[:14]:
            engine.feed(t)
        last = self.ticks[13]
        events = engine.feed(replace(last, ts=last.ts+31))
        self.assertEqual([e.kind for e in events], ["失效"])
        engine = Engine(self.c)
        for t in self.ticks[:8]:
            engine.feed(t)
        last = self.ticks[7]
        self.assertFalse(engine.feed(replace(last, ts=last.ts+20)))
        self.assertFalse(engine.states[last.code].watch_sent)

    def test_reset_on_day_and_volume_reset(self):
        engine = Engine(self.c)
        for t in self.ticks[:13]:
            engine.feed(t)
        reset = replace(self.ticks[13], volume=1)
        self.assertFalse(engine.feed(reset))
        self.assertEqual(engine.states[reset.code].phase, "search")
        tomorrow = replace(reset, ts=reset.ts+86400)
        self.assertFalse(engine.feed(tomorrow))
        self.assertEqual(len(engine.states[tomorrow.code].history), 1)

    def test_no_signal_when_near_limit_or_deep_pullback(self):
        engine = Engine(self.c)
        self.assertFalse([e for t in self.ticks for e in engine.feed(replace(t, prev=9.5))])
        engine = Engine(self.c)
        for t in self.ticks[:5]:
            engine.feed(t)
        engine.feed(replace(self.ticks[5], price=10.31))
        engine.feed(replace(self.ticks[6], price=10.30))
        self.assertEqual(engine.states[self.ticks[0].code].phase, "search")

    def test_no_lookahead(self):
        full, prefix = Engine(self.c), Engine(self.c)
        a = [asdict(e) for t in self.ticks[:12] for e in full.feed(t)]
        b = [asdict(e) for t in self.ticks[:12] for e in prefix.feed(t)]
        for t in self.ticks[12:]:
            full.feed(t)
        self.assertEqual(a, b)
        self.assertNotIn("入场候选", [e["kind"] for e in a])

    def test_main_board_filter(self):
        meta = dict(thscode="600000.SH", name="普通股票", list_date="1999-01-01")
        today = date(2026, 10, 26)
        self.assertTrue(main_board(meta, today))
        for change in (dict(name="*ST某股"), dict(name="退市某股"), dict(thscode="300001.SZ"),
                       dict(thscode="688001.SH"), dict(thscode="920001.BJ"), dict(list_date=None),
                       dict(list_date="2026-10-01"), dict(end_date="2020-01-01")):
            self.assertFalse(main_board(meta | change, today), change)

    def test_holdings_t_plus_one_and_profit_giveback(self):
        t = self.ticks[0]
        p = dict(buy_date="2026-10-26", cost=10, shares=100)
        self.assertIsNone(holding_event(p, replace(t, price=9), self.c))
        p["buy_date"] = "2026-10-23"
        self.assertIsNone(holding_event(p, replace(t, price=10.8), self.c))
        e = holding_event(p, replace(t, ts=t.ts+10, price=10.5), self.c)
        self.assertEqual(e.kind, "持仓转弱")
        self.assertIsNone(holding_event(p, replace(t, ts=t.ts+11, price=10.4), self.c))

    def test_holding_stop_loss_and_morning_observation(self):
        p = dict(buy_date="2026-10-23", cost=10, shares=100)
        t = replace(self.ticks[0], price=9.4)
        early = datetime(2026, 10, 26, 9, 31, tzinfo=CN).timestamp()
        self.assertIsNone(holding_event(p, replace(t, ts=early), self.c))
        self.assertEqual(holding_event(p, t, self.c).kind, "持仓转弱")

    def test_tick_rejects_invalid_numbers(self):
        row = dict(thscode="600000.SH", last_price=float("nan"), volume=1, turnover=1, prev_price=10, open_price=10)
        with self.assertRaises(ValueError):
            Tick.parse(row, 1, 1000, "测试")


class ClientTests(unittest.TestCase):
    def client(self, data=None, error=None):
        c = monitor.Client("test-placeholder-not-a-real-key")
        opener = unittest.mock.MagicMock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(data).encode()
        c.opener = opener
        return c

    def test_auth_header_and_valid_business_response(self):
        c = self.client(dict(code=0, data=dict(item=[], timestamp=None)))
        result, elapsed = c.get("/api/a-share/prices/snapshot", thscodes="600000.SH")
        self.assertEqual(result["item"], [])
        req = c.opener.open.call_args.args[0]
        self.assertEqual(req.get_header("X-api-key"), "test-placeholder-not-a-real-key")
        self.assertNotIn("test-placeholder", req.full_url)
        self.assertGreaterEqual(elapsed, 0)

    def test_invalid_key_and_permissions_are_fatal(self):
        for code in (2001, 2003):
            with self.assertRaises(monitor.ApiError) as caught:
                self.client(dict(code=code, message="do-not-log-server-text")).get("/api/a-share/prices/snapshot")
            self.assertFalse(caught.exception.retry)
            self.assertNotIn("do-not-log", str(caught.exception))

    def test_business_and_http_rate_limit_retry(self):
        clients = [self.client(dict(code=4001)), self.client(error=urllib.error.HTTPError("https://example.invalid", 429, "limit", {}, None))]
        for c in clients:
            with self.assertRaises(monitor.ApiError) as caught:
                c.get("/api/a-share/prices/snapshot")
            self.assertTrue(caught.exception.retry)

    def test_no_secret_redirect(self):
        req = monitor.urllib.request.Request("https://fuyao.aicubes.cn/api", headers={"X-api-key": "test"})
        self.assertIsNone(monitor.NoRedirect().redirect_request(req, None, 302, "", {}, "https://other.invalid"))


class RuntimeTests(unittest.TestCase):
    def test_session_clock_lunch_weekend(self):
        for clock, expected in (("09:25", "auction"), ("09:30", "market"), ("11:30", "closed"), ("13:00", "market"), ("15:00", "closed")):
            now = datetime.fromisoformat("2026-10-26T"+clock).replace(tzinfo=CN)
            self.assertEqual(monitor.session_phase(now), expected)
        self.assertEqual(monitor.session_phase(datetime(2026, 10, 25, 10, 0, tzinfo=CN)), "closed")

    def test_stale_batch_stored_but_not_used(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DATA", Path(directory)):
            messages = []
            m = monitor.Monitor(monitor.configuration(), lambda *v: messages.append(v), threading.Event())
            m.consume(dict(timestamp=1000, item=[]), 100, {}, set())
            self.assertFalse(m.engine.states)
            self.assertIn("过旧", messages[-1][1])

    def test_live_consumer_emits_and_saves_signals(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DATA", Path(directory)):
            messages = []
            m = monitor.Monitor(monitor.configuration(), lambda *v: messages.append(v), threading.Event())
            m.store = monitor.Storage("live")
            names = {"600000.SH": {"name": "测试股票"}}
            try:
                for t in monitor.demo_ticks():
                    row = dict(thscode=t.code, last_price=t.price, volume=t.volume, turnover=t.turnover, prev_price=t.prev, open_price=t.opening)
                    m.consume(dict(timestamp=t.batch_ms, item=[row]), t.ts, names, set())
                events = [v["kind"] for k, v in messages if k == "event"]
                self.assertEqual(events, ["观察", "入场候选", "失效"])
                self.assertEqual(m.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 3)
            finally:
                m.store.close()

    def test_positions_persist_and_remove(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DATA", Path(directory)):
            p = monitor.Positions()
            p.set("600000.SH", dict(cost=10, shares=100, buy_date="2026-10-23"))
            self.assertEqual(p.read()["600000.SH"]["cost"], 10)
            p.set("600000.SH", None)
            self.assertEqual(p.read(), {})

    def test_capture_and_replay_functional_workflow(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DATA", Path(directory)):
            store = monitor.Storage("live")
            for t in monitor.demo_ticks():
                row = dict(thscode=t.code, last_price=t.price, volume=t.volume, turnover=t.turnover, prev_price=t.prev, open_price=t.opening)
                store.frame("watch", dict(timestamp=t.batch_ms, item=[row]), t.ts, 0.1)
            store.close()
            result = io.StringIO()
            with contextlib.redirect_stdout(result):
                monitor.replay(Path(directory)/"market.sqlite3", monitor.configuration())
            self.assertIn("[观察]", result.getvalue())
            self.assertIn("[入场候选]", result.getvalue())
            self.assertIn("[失效]", result.getvalue())

    def test_configuration_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"config.json"
            for value in (dict(max_watch=101), dict(batch_size=1.5), dict(rise_pct=-1), dict(unknown=2)):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    monitor.configuration(path)


if __name__ == "__main__":
    unittest.main()
