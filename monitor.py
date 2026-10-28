"""Windows research monitor: Python standard library only; no trading API."""
import argparse
from dataclasses import asdict
from datetime import datetime
import getpass
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from strategy import CN, Engine, Tick, holding_event, limit_price, main_board, market_time
from market_context import MarketContext

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BASE = "https://fuyao.aicubes.cn"
POSITIONS_LOCK = threading.Lock()
VERSION = "0.2.1"


def configuration(path=None):
    c = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if path:
        override = json.loads(Path(path).read_text(encoding="utf-8"))
        if set(override)-set(c):
            raise ValueError("配置含未知字段")
        c.update(override)
    for k, v in c.items():
        if k == "context_enabled":
            if not isinstance(v, bool):
                raise ValueError("context_enabled 必须为布尔值")
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ValueError("配置必须是有限正数: " + k)
    for k in ("batch_size", "max_watch", "min_rise_samples", "min_stable_updates", "rebound_confirm_updates", "max_failed_breakouts"):
        if not isinstance(c[k], int):
            raise ValueError(k + " 必须为整数")
    if c["batch_size"] > 100 or c["max_watch"] > 100:
        raise ValueError("试验版每批/重点观察最多100只")
    if c["pullback_min_pct"] >= c["pullback_max_pct"] or c["min_rise_retention_ratio"] > 1:
        raise ValueError("阈值上下界错误")
    return c


class ApiError(Exception):
    def __init__(self, text, retry=False, code=None):
        super().__init__(text)
        self.retry = retry
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API key to a redirected destination.
        return None


class Client:
    def __init__(self, key):
        if not key:
            raise ApiError("未配置 FUYAO_API_KEY")
        self.key = key
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path, request_timeout=8, **params):
        if path not in ("/api/meta/tickers/list", "/api/a-share/prices/snapshot", "/api/a-share/auction/snapshot",
                        "/api/a-share/calendar/trading-days", "/api/a-share/special-data/limit-up-pool",
                        "/api/a-share/special-data/limit-up-statistics", "/api/a-share-index/prices/snapshot",
                        "/api/a-share-index/catalog/ths-index-list", "/api/a-share-index/constituents/ths-stock-list"):
            raise ApiError("未允许的接口")
        url = BASE + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-api-key": self.key, "User-Agent": "AShareResearchMonitor/0.1"})
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=request_timeout) as r:
                payload = json.loads(r.read(16*1024*1024))
        except urllib.error.HTTPError as e:
            raise ApiError("HTTP " + str(e.code), retry=e.code == 429 or e.code >= 500, code=e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError("网络连接失败或超时", retry=True) from None
        except (ValueError, UnicodeError):
            raise ApiError("接口返回非有效JSON") from None
        code = payload.get("code") if isinstance(payload, dict) else None
        if code != 0:
            label = {2001: "密钥无效或缺失", 2003: "接口权限不足", 4001: "请求限流"}.get(code, "业务接口错误")
            # No raw server error bodies: they may contain sensitive request details.
            raise ApiError(f"{label}（code={code if isinstance(code, int) else 'unknown'}）", retry=code == 4001, code=code)
        data = payload.get("data")
        if not isinstance(data, dict) or (path != "/api/a-share/special-data/limit-up-statistics" and not isinstance(data.get("item"), list)):
            raise ApiError("响应字段不符合文档：缺少 data.item")
        return data, time.monotonic()-started

    def universe(self, stop):
        rows, offset = {}, 0
        while not stop.is_set():
            data, _ = self.get("/api/meta/tickers/list", asset_type="a-share", limit=1000, offset=offset)
            for item in data["item"]:
                if main_board(item):
                    rows[item["thscode"]] = item
            if len(data["item"]) < 1000:
                return rows
            offset += 1000
            if offset > 20000:
                raise ApiError("代码表分页异常")
            stop.wait(1)
        return rows


class Storage:
    def __init__(self, mode):
        DATA.mkdir(exist_ok=True)
        self.db = sqlite3.connect(DATA / ("demo.sqlite3" if mode == "demo" else "market.sqlite3"))
        self.db.execute("CREATE TABLE IF NOT EXISTS frames (id INTEGER PRIMARY KEY, received REAL, kind TEXT, batch_ms INTEGER, elapsed REAL, payload TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, received REAL, payload TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (started REAL, mode TEXT, config TEXT)")

    def session(self, mode, config):
        self.db.execute("INSERT INTO sessions VALUES (?,?,?)", (time.time(), mode, json.dumps(dict(version=VERSION, parameters=config))))
        self.db.commit()

    def frame(self, kind, data, received, elapsed):
        self.db.execute("INSERT INTO frames(received,kind,batch_ms,elapsed,payload) VALUES (?,?,?,?,?)",
                        (received, kind, data.get("timestamp"), elapsed, json.dumps(data, ensure_ascii=False)))
        self.db.commit()

    def event(self, event):
        self.db.execute("INSERT INTO events(received,payload) VALUES (?,?)", (event.ts, json.dumps(asdict(event), ensure_ascii=False)))
        self.db.commit()

    def close(self):
        self.db.close()


class Positions:
    def __init__(self):
        DATA.mkdir(exist_ok=True)
        self.path = DATA / "positions.json"
        self.lock = POSITIONS_LOCK

    def read(self):
        if not self.path.exists():
            return {}
        result = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("持仓文件格式错误")
        for code, p in result.items():
            datetime.strptime(p["buy_date"], "%Y-%m-%d")
            if not math.isfinite(p["cost"]) or p["cost"] <= 0 or p["shares"] <= 0:
                raise ValueError("持仓数据无效: " + code)
        return result

    def write(self, rows):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def set(self, code, position):
        with self.lock:
            rows = self.read()
            if position is None:
                rows.pop(code, None)
            else:
                rows[code] = position
            self.write(rows)


def session_phase(now):
    if now.weekday() >= 5:
        return "closed"
    clock = now.strftime("%H:%M:%S")
    if "09:25:00" <= clock < "09:30:00":
        return "auction"
    if "09:30:00" <= clock < "11:30:00" or "13:00:00" <= clock < "15:00:00":
        return "market"
    return "closed"


def demo_ticks():
    start = datetime(2026, 10, 26, 10, 0, tzinfo=CN).timestamp()
    prices = [10.30, 10.36, 10.44, 10.52, 10.60, 10.56, 10.52, 10.50,
              10.50, 10.51, 10.51, 10.51, 10.52, 10.57, 10.59, 10.48]
    increments = [10000]*5 + [1000]*8 + [20000]*3
    volume = 100000
    turnover = 1000000
    for i, (price, delta) in enumerate(zip(prices, increments)):
        volume += delta
        turnover += price*delta
        ts = start+i*5
        yield Tick("600000.SH", "模拟股票（非推荐）", ts, int(ts*1000), price, volume, turnover, 10, 10.3)


class Monitor:
    def __init__(self, config, emit, stop, key="", mode="live", max_cycles=None, codes=None):
        self.c, self.emit, self.stop = config, emit, stop
        self.key, self.mode, self.max_cycles = key, mode, max_cycles
        self.codes = codes
        self.engine = Engine(config)
        self.positions = Positions()
        self.market_context = MarketContext(config, DATA)
        self.store = None
        self.stats = {"universe": 0, "watch": 0, "requests": 0}
        self.request_scope = "初始化"

    def connection_issue(self, message, wait=0, blocking=True):
        message = str(message).replace(self.key, "[已隐藏]") if self.key else str(message)
        message = re.sub(r"sk-fuyao-[A-Za-z0-9_-]+", "[已隐藏]", message)
        issue = dict(time=datetime.now(CN).isoformat(timespec="seconds"), stage=self.request_scope,
                     message=message, wait=wait, blocking=blocking, version=VERSION)
        self.emit("connection", issue)
        try:
            with (DATA / "connection.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(issue, ensure_ascii=False)+"\n")
        except OSError:
            pass

    def quote_view(self, t, position=None):
        s = self.engine.states.get(t.code)
        history = list(s.history) if s else []
        first = next((x for x in history if t.ts-x.ts <= 60), t)
        speed = (t.price/first.price-1)*100 if t.ts > first.ts else None
        last = history[-2] if len(history) > 1 else None
        rate = (t.volume-last.volume)/(t.ts-last.ts) if last and t.ts > last.ts else None
        return dict(**asdict(t), phase=s.phase if s else "search", ready=bool(s and s.watch_sent),
                    speed=speed, volume_ratio=s.last_ratio if s and s.last_ratio else None,
                    pullback=(1-t.price/s.peak)*100 if s and s.peak else None,
                    vwap=t.turnover/t.volume if t.volume else None,
                    distance=(limit_price(t.prev)/t.price-1)*100,
                    low=s.low if s else None, confirming=bool(s and s.confirm_updates),
                    position=dict(position) if position else None,
                    context=self.market_context.stock_view(t.code, t.ts, t.gain))

    def send_event(self, event):
        event.metrics["version"] = VERSION
        event.metrics["context"] = self.market_context.view(event.code, event.ts, event.metrics.get("gain"))
        self.store.event(event)
        self.emit("event", asdict(event))

    def consume(self, data, received, names, held):
        stamp = data.get("timestamp")
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            self.emit("status", "批次缺少有效行情时间，已存档但不生成信号")
            return False
        age = received-stamp/1000
        if age > self.c["max_batch_age_seconds"] or age < -5:
            self.emit("status", f"批次行情时间异常/过旧（{age:.1f}秒），不生成信号")
            return False
        with self.positions.lock:
            positions = self.positions.read()
            views = []
            for row in data["item"]:
                code = row.get("thscode")
                if code not in names:
                    continue
                try:
                    t = Tick.parse(row, received, stamp, names[code]["name"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    self.emit("status", f"跳过无效行情: {code}")
                    continue
                if code not in held:
                    for e in self.engine.feed(t):
                        self.send_event(e)
                if code in positions:
                    e = holding_event(positions[code], t, self.c)
                    if e:
                        self.send_event(e)
                views.append(self.quote_view(t, positions.get(code)))
            if positions:
                self.positions.write(positions)
        if views:
            self.emit("quotes", views)
        return True

    def run(self):
        try:
            self.emit("lifecycle", "loading")
            self.emit("status", "正在准备本地行情存储并读取股票代码表")
            self.store = Storage(self.mode)
            self.store.session(self.mode, self.c)
            if self.mode == "demo":
                for t in demo_ticks():
                    if self.stop.is_set():
                        break
                    self.store.frame("demo", {"timestamp": t.batch_ms, "item": [asdict(t)]}, t.ts, 0)
                    self.emit("status", f"演示 {market_time(t.ts):%H:%M:%S} 价格 {t.price:.2f}，使用合成数据")
                    for e in self.engine.feed(t):
                        self.send_event(e)
                    self.emit("quotes", [self.quote_view(t)])
                    self.emit("lifecycle", "demo")
                    self.stop.wait(0.35)
                return
            client = Client(self.key)
            self.request_scope = "股票代码表"
            startup_backoff = 2
            while not self.stop.is_set():
                try:
                    names = client.universe(self.stop)
                    break
                except ApiError as e:
                    if not e.retry:
                        raise
                    self.emit("lifecycle", "retrying")
                    self.connection_issue(str(e), startup_backoff)
                    self.emit("status", f"代码表：{e}，{startup_backoff}秒后重试")
                    self.stop.wait(startup_backoff)
                    startup_backoff = min(startup_backoff*2, 120)
            if self.stop.is_set():
                return
            if self.codes:
                names = {k: v for k, v in names.items() if k in self.codes}
            if not names:
                raise ApiError("没有符合主板、非ST、上市满60自然日条件的股票，或代码表权限/字段不满足要求")
            self.stats["universe"] = len(names)
            self.emit("stats", dict(self.stats))
            self.emit("status", f"股票池 {len(names)} 只；重点观察最多 {self.c['max_watch']} 只；尚未验证交易效果")
            self.loop(client, names)
        except ApiError as e:
            self.emit("lifecycle", "error")
            self.connection_issue(str(e))
            self.emit("error", f"{self.request_scope}：{e}")
        except Exception as e:
            self.emit("lifecycle", "error")
            # Deliberately no exception text/traceback containing headers or secrets.
            message = f"本地处理失败（{type(e).__name__}），请检查配置、持仓文件及磁盘权限"
            self.connection_issue(message)
            self.emit("error", message)
        finally:
            self.key = ""
            if self.store:
                self.store.close()
            self.emit("done", "监控已停止")

    def loop(self, client, names):
        codes = sorted(names)
        cursor, candidates, auction_done = 0, {}, set()
        next_watch = next_scan = 0
        day = None
        backoff, cycles = 2, 0
        scan_started = time.monotonic()
        while not self.stop.is_set():
            now = datetime.now(CN)
            if str(now.date()) != day:
                if day is not None:
                    # Refresh stock names/ST/listing status every trading day; fail closed on error.
                    raise ApiError("交易日已变化，请重新启动以刷新股票池与ST状态")
                day = str(now.date())
            phase = session_phase(now)
            calendar = self.market_context.calendar_days
            if self.c["context_enabled"] and calendar is not None and now.date() not in calendar:
                phase = "closed"
            if phase == "closed":
                self.emit("lifecycle", "paused")
                self.emit("status", "当前休市，保持运行将于下一交易时段继续；午休后重新观察盘中结构。")
                self.stop.wait(30)
                continue
            try:
                self.request_scope = "本地持仓"
                with self.positions.lock:
                    held = set(self.positions.read())
                if held-set(names):
                    raise ApiError("持仓含当前股票池以外代码；请清理或另行人工监控，程序不静默忽略")
                mono = time.monotonic()
                if phase == "auction":
                    batch = [k for k in codes if k not in auction_done][:self.c["batch_size"]]
                    if not batch:
                        self.stop.wait(5)
                        continue
                    self.request_scope = "竞价行情"
                    data, elapsed = client.get("/api/a-share/auction/snapshot", thscodes=",".join(batch), stage="final")
                    received = time.time()
                    self.store.frame("auction", data, received, elapsed)
                    if data.get("data_status") == "ready":
                        auction_done.update(batch)
                        for row in data["item"]:
                            code = row.get("thscode")
                            pct = row.get("auction_pct")
                            if code in names and isinstance(pct, (int, float)) and pct >= self.c["candidate_gap_pct"]:
                                candidates[code] = (pct, received)
                        self.emit("status", f"竞价已记录 {len(auction_done)}/{len(codes)} 只；只入观察池，不发买入信号")
                    else:
                        self.emit("status", "竞价终态尚未就绪，稍后重试")
                        self.stop.wait(5)
                    self.stop.wait(self.c["scan_interval_seconds"])
                    continue
                # Holdings take priority; candidate list is refreshed by rolling market scans.
                candidates = {k: v for k, v in candidates.items() if time.time()-v[1] < 300}
                ranked = sorted(candidates, key=lambda k: candidates[k][0], reverse=True)
                watch = sorted(held) + [k for k in ranked if k not in held][:max(0, self.c["max_watch"]-len(held))]
                if len(held) > self.c["max_watch"]:
                    raise ApiError("持仓数量超过重点观察上限")
                is_watch = bool(watch) and mono >= next_watch
                if is_watch:
                    batch = watch
                    next_watch = mono+self.c["watch_interval_seconds"]
                elif self.market_context.due(mono):
                    self.request_scope = "附加市场信息"
                    extra = self.market_context.step(client, time.time(), mono)
                    if extra and "data" in extra:
                        self.store.frame("context:"+extra["task"], extra["data"], time.time(), extra["elapsed"])
                        self.stats["requests"] += 1
                    if extra and "error" in extra:
                        self.request_scope = "附加信息/"+extra["task"]
                        self.connection_issue(extra["error"], blocking=False)
                    self.emit("context", self.market_context.view())
                    self.stop.wait(0.5)
                    continue
                elif mono >= next_scan:
                    batch = codes[cursor:cursor+self.c["batch_size"]]
                    cursor += len(batch)
                    next_scan = mono+self.c["scan_interval_seconds"]
                    if cursor >= len(codes):
                        cursor = 0
                        self.emit("status", f"一轮全池扫描调度耗时 {mono-scan_started:.1f}秒；重点池 {len(watch)}只")
                        scan_started = mono
                else:
                    self.stop.wait(0.2)
                    continue
                self.request_scope = "重点股行情" if is_watch else "全池行情扫描"
                data, elapsed = client.get("/api/a-share/prices/snapshot", thscodes=",".join(batch))
                received = time.time()
                data["_context"] = {"held": sorted(held), "names": {k: names[k]["name"] for k in batch}}
                self.store.frame("watch" if is_watch else "scan", data, received, elapsed)
                if self.stop.is_set():
                    return
                for row in data["item"]:
                    code = row.get("thscode")
                    if code not in names:
                        continue
                    try:
                        prev, price, opening = (float(row[k]) for k in ("prev_price", "last_price", "open_price"))
                        gain, gap = (price/prev-1)*100, (opening/prev-1)*100
                        if not all(math.isfinite(v) for v in (gain, gap)):
                            continue
                        if gain >= self.c["candidate_gain_pct"] or gap >= self.c["candidate_gap_pct"]:
                            candidates[code] = (gain+max(gap, 0)*0.2, received)
                        else:
                            candidates.pop(code, None)
                    except (ValueError, TypeError, KeyError, ZeroDivisionError):
                        continue
                valid = self.consume(data, received, names, held)
                self.stats.update(watch=len(watch), requests=self.stats["requests"]+1,
                                  received=received, elapsed=elapsed, batch_ms=data.get("timestamp"))
                self.emit("stats", dict(self.stats))
                self.emit("lifecycle", "running" if valid else "stale")
                stamp = data.get("timestamp")
                age = f"{received-stamp/1000:.1f}秒" if isinstance(stamp, (float, int)) else "未知"
                if valid:
                    self.emit("status", f"{'重点' if is_watch else '扫描'} {len(data['item'])}只 | 请求{elapsed:.2f}秒 | 批次最新行情距接收{age}（不代表每只股票）")
                cycles += 1
                backoff = 2
                if self.max_cycles and cycles >= self.max_cycles:
                    return
                self.stop.wait(0.5)  # Serial requests; never burst retries.
            except ApiError as e:
                if not e.retry:
                    raise
                self.emit("lifecycle", "retrying")
                self.connection_issue(str(e), backoff)
                self.emit("status", f"{self.request_scope}：{e}；退避{backoff}秒；采样中断时旧结构会重置")
                self.stop.wait(backoff)
                backoff = min(backoff*2, 120)


def show_event(e):
    clock = market_time(e["ts"]).strftime("%H:%M:%S")
    batch = market_time(e["batch_ms"]/1000).strftime("%H:%M:%S") if e["batch_ms"] else "未知"
    return (f"[{e['kind']}] {e['name']} {e['code']}  ¥{e['price']:.2f}\n"
            f"信号接收时间 {clock}｜批次最新行情 {batch}\n{e['reason']}" +
            (f"\n承接低点参考 ¥{e['low']:.2f}" if e["low"] else ""))


def gui(config):
    import sys
    from dashboard import run
    run(config, sys.modules[__name__])


def replay(path, config):
    # Read-only replay of raw quotes; no fills, profits or success-rate claims.
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)
    engine, counts = Engine(config), {}
    try:
        for received, batch_ms, payload in db.execute("SELECT received,batch_ms,payload FROM frames WHERE kind IN ('scan','watch') ORDER BY received,id"):
            if not batch_ms or not -5 <= received-batch_ms/1000 <= config["max_batch_age_seconds"]:
                continue
            data = json.loads(payload)
            context = data.get("_context", {})
            for row in data["item"]:
                if row.get("thscode") in context.get("held", []):
                    continue
                try:
                    t = Tick.parse(row, received, batch_ms, context.get("names", {}).get(row["thscode"], row["thscode"]))
                except (KeyError, ValueError, TypeError):
                    continue
                for e in engine.feed(t):
                    counts[e.kind] = counts.get(e.kind, 0)+1
                    print(show_event(asdict(e)), "\n")
    finally:
        db.close()
    print("信号回放数量（不是交易回测）：", json.dumps(counts, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="合成数据控制台演示，无需密钥")
    mode.add_argument("--live", action="store_true", help="控制台实时监控")
    mode.add_argument("--probe", action="store_true", help="仅请求一次单股行情，检查权限与字段")
    mode.add_argument("--replay", metavar="SQLITE", help="回放自己采集的原始行情，不计算收益")
    parser.add_argument("--config", help="JSON配置覆盖文件")
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument("--codes", help="仅监控指定主板股票，逗号分隔；默认滚动扫描全池")
    args = parser.parse_args()
    config = configuration(args.config)
    if args.replay:
        replay(args.replay, config)
        return
    if not (args.demo or args.live or args.probe):
        gui(config)
        return
    key = ""
    if not args.demo:
        key = os.environ.get("FUYAO_API_KEY", "") or getpass.getpass("扶摇密钥（不回显、不保存）：")
    if args.probe:
        try:
            data, elapsed = Client(key).get("/api/a-share/prices/snapshot", thscodes="600000.SH")
            print(json.dumps({"elapsed_seconds": round(elapsed, 3), "data": data}, ensure_ascii=False, indent=2))
        except ApiError as e:
            parser.exit(1, str(e)+"\n")
        return
    stop = threading.Event()
    failed = []
    def emit(kind, value):
        if kind == "error":
            failed.append(value)
        if kind in ("event", "status", "error", "done"):
            print(show_event(value) if kind == "event" else value, flush=True)
    try:
        Monitor(config, emit, stop, key, "demo" if args.demo else "live", args.max_cycles,
                set(args.codes.upper().split(",")) if args.codes else None).run()
    except KeyboardInterrupt:
        stop.set()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
