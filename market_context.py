"""Optional, timestamped market evidence. Never used as an entry gate."""
from datetime import datetime
import json
import math
import re
import time

from strategy import CN, market_time


class MarketContext:
    def __init__(self, config, directory, today=None):
        self.c = config
        self.today = today or datetime.now(CN).date()
        self.directory = directory
        self.next = {k: 0 for k in ("calendar", "previous", "stats", "catalog", "indices", "members")}
        self.next_request = 0
        self.calendar_days, self.previous_day = None, None
        self.previous, self.previous_complete, self.previous_page = {}, False, 1
        self.sectors, self.members, self.indices = {}, {}, {}
        self.market, self.errors = None, {}
        self.failures = {}
        self.index_cursor = 0
        self.cache = directory / f"industry-members-{self.today}.json"
        if self.cache.exists():
            try:
                cached = json.loads(self.cache.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    self.members = {k: v for k, v in cached.items() if isinstance(k, str) and k.endswith(".TI") and
                                    isinstance(v, list) and all(isinstance(x, str) and re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", x) for x in v)}
            except (ValueError, OSError):
                self.errors["members"] = "行业缓存未能读取，重新获取"

    def due(self, mono):
        return self.c["context_enabled"] and mono >= self.next_request and any(mono >= v for v in self.next.values())

    def step(self, client, now, mono=None):
        mono = time.monotonic() if mono is None else mono
        self.next_request = mono+5
        task = next((k for k, v in self.next.items() if mono >= v), None)
        if task is None:
            return None
        self.next[task] = mono+self.c["context_interval_seconds"]
        if task == "previous" and not self.previous_day:
            self.next[task] = math.inf
            return None
        if task == "members":
            missing = sorted(set(self.sectors)-set(self.members))
            if not missing:
                self.next[task] = math.inf
                return None
            sector = missing[0]
        if task == "indices":
            sector_codes = sorted(self.sectors)
            batch = sector_codes[self.index_cursor:self.index_cursor+98]
            self.index_cursor = self.index_cursor+len(batch) if self.index_cursor+len(batch) < len(sector_codes) else 0
        path, params = {
            "calendar": ("/api/a-share/calendar/trading-days", {}),
            "previous": ("/api/a-share/special-data/limit-up-pool", dict(date_ms=self.previous_day, page=self.previous_page, size=200)),
            "stats": ("/api/a-share/special-data/limit-up-statistics", dict(date=str(self.today))),
            "catalog": ("/api/a-share-index/catalog/ths-index-list", dict(tag="industry")),
            "indices": ("/api/a-share-index/prices/snapshot", dict(thscodes=",".join(["000001.SH", "399001.SZ"]+(batch if task == "indices" else [])))),
            "members": ("/api/a-share-index/constituents/ths-stock-list", dict(thscode=sector if task == "members" else "")),
        }[task]
        try:
            data, elapsed = client.get(path, request_timeout=3, **params)
        except Exception as e:
            if getattr(e, "code", None) == 2001:
                raise
            if getattr(e, "code", None) in (429, 4001):
                # Stop all optional requests during the shared rate-limit cooldown.
                self.next_request = mono+max(60, self.c["context_interval_seconds"])
                self.errors[task] = "附加信息限流，已暂停附加请求并对全局退避"
                raise
            self.errors[task] = str(e) if hasattr(e, "retry") else "附加数据暂不可用"
            if getattr(e, "retry", False):
                self.failures[task] = self.failures.get(task, 0)+1
                self.next[task] = mono+min(600, self.c["context_interval_seconds"]*2**min(self.failures[task]-1, 3))
            else:
                self.next[task] = math.inf
            return {"task": task, "error": self.errors[task]}
        try:
            self.accept(task, data, now, mono, sector if task == "members" else None)
            self.errors.pop(task, None)
            self.failures.pop(task, None)
        except (ValueError, KeyError, TypeError, OverflowError):
            self.errors[task] = "附加数据字段或日期不完整，未用于判断"
            self.next[task] = math.inf
        return {"task": task, "data": data, "elapsed": elapsed}

    def accept(self, task, data, now, mono, sector=None):
        if task == "calendar":
            dates = {datetime.strptime(r["date"], "%Y%m%d").date() for r in data["item"]}
            if not dates or any(d > self.today for d in dates):
                raise ValueError()
            self.calendar_days = dates
            prior = sorted(d for d in dates if d < self.today)
            if prior:
                previous = prior[-1]
                self.previous_day = int(datetime(previous.year, previous.month, previous.day, tzinfo=CN).timestamp()*1000)
            self.next[task] = math.inf
        elif task == "previous":
            pagination = data["pagination"]
            if pagination["page"] != self.previous_page or pagination["pages"] > 100:
                raise ValueError()
            for row in data["item"]:
                self.previous[row["thscode"]] = row.get("continue_day_cnt")
            if self.previous_page < pagination["pages"]:
                self.previous_page += 1
                self.next[task] = mono+5
            else:
                self.previous_complete = len(self.previous) == pagination["total"]
                self.next[task] = math.inf
                if not self.previous_complete:
                    raise ValueError()
        elif task == "stats":
            if data.get("trade_date") != str(self.today):
                self.market = None
                raise ValueError()
            self.market = data
        elif task == "catalog":
            self.sectors = {r["thscode"]: r["name"] for r in data["item"] if isinstance(r.get("thscode"), str) and r["thscode"].endswith(".TI")}
            self.next[task], self.next["indices"], self.next["members"] = math.inf, 0, 0
        elif task == "members":
            self.members[sector] = [r["thscode"] for r in data["item"]]
            self.next[task] = mono+5
            try:
                temporary = self.cache.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.members, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.cache)
            except OSError:
                self.errors["cache"] = "行业映射仅保留本次内存"
        elif task == "indices":
            stamp = data.get("timestamp")
            if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                raise ValueError()
            for row in data["item"]:
                gain = row.get("price_change_ratio_pct")
                if isinstance(gain, (int, float)) and math.isfinite(gain):
                    self.indices[row["thscode"]] = dict(gain=gain, timestamp=stamp)
        else:
            raise ValueError()

    def fresh(self, stamp, now):
        return isinstance(stamp, (int, float)) and math.isfinite(stamp) and -5 <= now-stamp/1000 <= self.c["context_interval_seconds"]*2

    def stock_view(self, code, now, gain):
        view = self.view(code, now, gain)
        return {k: view[k] for k in ("enabled", "pattern", "sectors", "mapping")}

    def view(self, code=None, now=None, gain=None):
        now = time.time() if now is None else now
        if not self.c["context_enabled"]:
            return dict(enabled=False, indices={}, market=None, sectors=[], pattern="首板/连板待核实", mapping="已关闭", errors={})
        indices = {k: dict(v, name=self.sectors.get(k, {"000001.SH": "上证", "399001.SZ": "深成"}.get(k, k)))
                   for k, v in self.indices.items() if self.fresh(v.get("timestamp"), now)}
        market = self.market if self.market and self.fresh(self.market.get("timestamp"), now) else None
        boards = [dict(code=k, name=self.sectors[k], gain=indices.get(k, {}).get("gain"),
                       relative_gain=gain-indices[k]["gain"] if gain is not None and k in indices else None)
                  for k in self.sectors if code in self.members.get(k, [])] if code else []
        if self.previous_complete and code:
            count = self.previous.get(code)
            pattern = (f"昨日{count}连板 · 晋级观察" if isinstance(count, int) and count > 0 else "昨日涨停 · 板数待核实") if code in self.previous else "昨日非涨停 · 首板候选"
        else:
            pattern = "首板/连板待核实"
        return dict(enabled=self.c["context_enabled"], indices=indices, market=market, sectors=boards, pattern=pattern,
                    mapping=f"{len(set(self.members)&set(self.sectors))}/{len(self.sectors)}", errors=dict(self.errors))
