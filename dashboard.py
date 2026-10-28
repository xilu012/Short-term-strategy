"""Native desktop dashboard. Pure view model is also usable without a display."""
from collections import Counter, deque
from datetime import datetime
import math
import os
import queue
import re
import threading
import time

from strategy import CN, market_time

BG, PANEL, TEXT, MUTED = "#101827", "#192438", "#e8eef7", "#95a7bf"
RED, GREEN, AMBER, BLUE = "#ff767d", "#5bd3a2", "#f5c66e", "#6ea8fe"
STAGES = {"search": "扫描", "rising": "快速拉升", "pullback": "回落观察", "signaled": "入场候选", "cooldown": "已失效"}
STATES = {"idle": "尚未启动", "loading": "连接数据源", "running": "监控中", "demo": "演示中", "stale": "行情过旧 · 暂停信号",
          "paused": "休市等待", "retrying": "连接重试", "stopping": "正在停止", "stopped": "已停止", "error": "连接异常"}


def number(value, digits=2, suffix="", signed=False):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return format(value, ("+" if signed else "")+f".{digits}f")+suffix


class DashboardModel:
    def __init__(self, config):
        self.config = config
        self.clear("live")

    def clear(self, mode):
        self.mode, self.state, self.day = mode, "idle", None
        self.quotes, self.histories, self.latest = {}, {}, {}
        self.events = deque(maxlen=500)
        self.counts = Counter()
        self.stats, self.context = {}, {}
        self.status = "点击启动监控；演示可从工具菜单运行。"
        self.last_error, self.last_connection = "", {}

    def ingest(self, kind, value):
        if kind == "quotes":
            for row in value:
                day = market_time(row["ts"]).date()
                if self.day is not None and day != self.day:
                    self.quotes.clear(); self.histories.clear(); self.latest.clear(); self.counts.clear()
                self.day = day
                code = row["code"]
                old = self.quotes.get(code)
                self.quotes[code] = row
                history = self.histories.setdefault(code, deque(maxlen=360))
                if not old or (old["price"], old["volume"], old["turnover"]) != (row["price"], row["volume"], row["turnover"]):
                    history.append({k: row.get(k) for k in ("ts", "price", "volume", "turnover", "vwap")})
        elif kind == "event":
            self.latest[value["code"]] = value
            self.events.appendleft(value)
            self.counts[value["kind"]] += 1
        elif kind == "stats":
            self.stats.update(value)
        elif kind == "context":
            self.context = value
        elif kind == "lifecycle":
            self.state = value
        elif kind == "connection":
            self.last_connection = value
        elif kind in ("status", "error", "done"):
            if kind == "done" and self.state == "error":
                return
            self.status = value
            if kind == "error":
                self.state = "error"
                self.last_error = value
            elif kind == "done" and self.state != "error":
                self.state = "stopped"

    def stage(self, row, now):
        if row.get("position"):
            return "持仓"
        if self.mode != "demo" and self.state in ("paused", "retrying", "error", "stopped", "stopping", "stale"):
            return "暂停确认"
        if self.mode != "demo" and now-row["ts"] > self.config["max_batch_age_seconds"]:
            return "待更新"
        stage = STAGES.get(row.get("phase"), "扫描")
        event = self.latest.get(row["code"])
        clock = row["ts"] if self.mode == "demo" else now
        if stage == "入场候选" and event and clock-event["ts"] > self.config["signal_expiry_seconds"]:
            return "已过期"
        if row.get("phase") == "pullback" and row.get("ready"):
            return "突破确认" if row.get("confirming") else "承接观察"
        return stage

    def rows(self, selection="全部候选", search="", now=None):
        now = now or time.time()
        result = []
        priority = {"入场候选": 0, "突破确认": 1, "承接观察": 2, "回落观察": 3, "快速拉升": 4}
        for row in self.quotes.values():
            code = row["code"]
            if search and search.casefold() not in (row["name"]+code).casefold():
                continue
            stage = self.stage(row, now)
            gain = (row["price"]/row["prev"]-1)*100
            gap = (row["opening"]/row["prev"]-1)*100
            candidate = (gain >= self.config["candidate_gain_pct"] or gap >= self.config["candidate_gap_pct"] or
                         code in self.latest or row.get("phase") in ("rising", "pullback", "signaled"))
            if selection == "持仓":
                if not row.get("position"):
                    continue
            elif selection != "全部候选":
                if stage != selection:
                    continue
            elif not candidate or row.get("position"):
                continue
            result.append((priority.get(stage, 10), -gain, code, row, stage))
        result.sort(key=lambda x: x[:3])
        return [(x[3], x[4]) for x in result[:200]]


class Dashboard:
    def __init__(self, window, config, service):
        import tkinter as tk
        from tkinter import ttk
        from tkinter import font as tkfont
        available = set(tkfont.families(window))
        self.font = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Micro Hei") if name in available), "Arial")
        self.tk, self.ttk, self.window, self.service = tk, ttk, window, service
        self.config, self.model = config, DashboardModel(config)
        self.messages, self.stop = queue.Queue(), threading.Event()
        self.worker, self.selected, self.popups = None, None, {}
        self.key = tk.StringVar(value=os.environ.get("FUYAO_API_KEY", ""))
        self.context_enabled = tk.BooleanVar(value=config["context_enabled"])
        self.filter = tk.StringVar(value="全部候选")
        self.search = tk.StringVar()
        self.banner = tk.StringVar(value=STATES["idle"])
        self.status = tk.StringVar(value=self.model.status)
        self.market = tk.StringVar(value="市场背景：等待采样")
        self.cards = {k: tk.StringVar(value="—") for k in ("更新", "股票池", "观察池", "候选", "请求")}
        self.detail_title = tk.StringVar(value="选择一只股票")
        self.detail_metrics = tk.StringVar(value="行情开始采集后显示分时走势")
        self.detail_reason = tk.StringVar()
        self.dirty = True
        window.title("A股短线雷达")
        window.geometry("1380x880")
        window.minsize(1080, 700)
        window.configure(bg=BG)
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=(self.font, 10))
        style.configure("TButton", background="#263c5b", foreground=TEXT, padding=(12, 7), borderwidth=0)
        style.map("TButton", background=[("active", "#365b87"), ("disabled", "#182336")])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=33, borderwidth=0, font=(self.font, 10))
        style.configure("Treeview.Heading", background="#23334d", foreground=MUTED, padding=8)
        style.map("Treeview", background=[("selected", "#2d4d70")], foreground=[("selected", "white")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), background=PANEL, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", "#294569")], foreground=[("selected", TEXT)])
        self.build()
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.pump_id = window.after(100, self.pump)

    def build(self):
        tk, ttk = self.tk, self.ttk
        header = ttk.Frame(self.window, padding=(18, 12))
        header.pack(fill="x")
        ttk.Label(header, text="A股短线雷达", font=(self.font, 20, "bold")).pack(side="left")
        self.state_label = ttk.Label(header, textvariable=self.banner, foreground=MUTED)
        self.state_label.pack(side="left", padx=25)
        self.start_button = ttk.Button(header, text="启动监控", command=lambda: self.start("live"))
        self.start_button.pack(side="right", padx=5)
        self.stop_button = ttk.Button(header, text="停止", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="right", padx=5)
        menu = tk.Menu(header, tearoff=False)
        menu.add_command(label="合成行情演示", command=lambda: self.start("demo"))
        menu.add_command(label="测试提示音", command=self.sound)
        menu.add_command(label="数据连接状态", command=self.connection_details)
        menu.add_checkbutton(label="市场与行业背景", variable=self.context_enabled, command=self.toggle_context)
        menu.add_command(label="指标与使用说明", command=self.help)
        button = ttk.Button(header, text="工具 ▾", command=lambda: menu.tk_popup(button.winfo_rootx(), button.winfo_rooty()+button.winfo_height()))
        button.pack(side="right", padx=5)
        connection = ttk.Frame(self.window, padding=(18, 0, 18, 10))
        connection.pack(fill="x")
        ttk.Label(connection, text="扶摇连接").pack(side="left", padx=(0, 12))
        ttk.Entry(connection, textvariable=self.key, show="●", width=48).pack(side="left")
        ttk.Label(connection, text="密钥仅本次使用", foreground=MUTED).pack(side="left", padx=12)
        ttk.Button(connection, text="管理持仓", command=self.position_dialog).pack(side="right")
        cards = ttk.Frame(self.window, padding=(18, 0, 18, 10))
        cards.pack(fill="x")
        for label, value in self.cards.items():
            box = tk.Frame(cards, bg=PANEL, padx=15, pady=10)
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(box, text=label, bg=PANEL, fg=MUTED, font=(self.font, 10)).pack(anchor="w")
            tk.Label(box, textvariable=value, bg=PANEL, fg=TEXT, font=(self.font, 16, "bold")).pack(anchor="w")
        ttk.Label(self.window, textvariable=self.market, foreground=MUTED, padding=(20, 4)).pack(fill="x")
        # Reserve footer space before the expanding panes, including at Windows DPI scaling.
        self.status_bar = ttk.Label(self.window, textvariable=self.status, foreground=MUTED,
                                    wraplength=1000, padding=(18, 8))
        self.status_bar.pack(side="bottom", fill="x")
        body = ttk.Panedwindow(self.window, orient="horizontal")
        body.pack(fill="both", expand=True, padx=18, pady=10)
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=3); body.add(right, weight=2)
        controls = ttk.Frame(left)
        controls.pack(fill="x", pady=(0, 8))
        combo = ttk.Combobox(controls, state="readonly", textvariable=self.filter, width=13,
                             values=("全部候选", "入场候选", "突破确认", "承接观察", "回落观察", "快速拉升", "持仓"))
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _: self.mark_dirty())
        ttk.Entry(controls, textvariable=self.search, width=20).pack(side="left", padx=8)
        ttk.Label(controls, text="搜索名称 / 代码", foreground=MUTED).pack(side="left")
        self.search.trace_add("write", lambda *_: self.mark_dirty())
        notebook = ttk.Notebook(left)
        notebook.pack(fill="both", expand=True)
        radar, alerts = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(radar, text="候选与持仓")
        notebook.add(alerts, text="提醒记录")
        columns = ("name", "code", "stage", "price", "gain", "speed", "ratio", "distance")
        self.table = ttk.Treeview(radar, columns=columns, show="headings", selectmode="browse")
        labels = ("名称", "代码", "阶段", "价格", "涨幅", "近60秒", "量能倍数", "距参考板")
        widths = (100, 95, 88, 65, 65, 70, 70, 80)
        for col, label, width in zip(columns, labels, widths):
            self.table.heading(col, text=label)
            self.table.column(col, width=width, minwidth=55, anchor="w" if col in ("name", "code", "stage") else "e")
        self.table.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(radar, command=self.table.yview)
        scroll.pack(side="right", fill="y"); self.table.configure(yscrollcommand=scroll.set)
        for name, color in (("入场候选", RED), ("承接观察", AMBER), ("持仓", BLUE), ("已失效", MUTED), ("待更新", MUTED), ("已过期", MUTED)):
            self.table.tag_configure(name, foreground=color)
        self.table.bind("<<TreeviewSelect>>", self.select)
        self.alerts = ttk.Treeview(alerts, columns=("time", "kind", "name", "price"), show="headings")
        for col, label in zip(("time", "kind", "name", "price"), ("时间", "提醒", "股票", "触发价格")):
            self.alerts.heading(col, text=label)
            self.alerts.column(col, width=100)
        self.alerts.pack(fill="both", expand=True)
        self.alerts.bind("<<TreeviewSelect>>", self.select_alert)
        ttk.Label(right, textvariable=self.detail_title, font=(self.font, 17, "bold"), padding=(15, 4)).pack(anchor="w")
        ttk.Label(right, textvariable=self.detail_metrics, wraplength=470, padding=(15, 4)).pack(anchor="w")
        self.chart = tk.Canvas(right, bg=PANEL, highlightthickness=0, height=300)
        self.chart.pack(fill="both", expand=True, padx=(12, 0), pady=10)
        self.chart.bind("<Configure>", lambda _: self.draw_chart())
        ttk.Label(right, text="触发依据与关键价位", foreground=MUTED, padding=(15, 5)).pack(anchor="w")
        ttk.Label(right, textvariable=self.detail_reason, wraplength=470, padding=(15, 8)).pack(fill="x")

    def mark_dirty(self):
        self.dirty = True

    def toggle_context(self):
        self.config["context_enabled"] = self.context_enabled.get()
        self.model.status = "市场背景已开启" if self.config["context_enabled"] else "市场背景已关闭，个股监控继续"
        self.mark_dirty()

    def start(self, mode):
        from tkinter import messagebox
        if self.worker and self.worker.is_alive():
            return
        if mode == "live" and not self.key.get().strip():
            messagebox.showinfo("数据连接", "请填写扶摇密钥")
            return
        while not self.messages.empty():
            try: self.messages.get_nowait()
            except queue.Empty: break
        self.model.clear(mode)
        self.selected = None
        for code in list(self.popups):
            self.dismiss(code)
        self.stop = threading.Event()
        monitor = self.service.Monitor(self.config, lambda k, v: self.messages.put((k, v)), self.stop, self.key.get().strip(), mode)
        self.worker = threading.Thread(target=monitor.run, daemon=True)
        self.model.state = "loading"
        self.model.status = "正在准备存储并连接数据源"
        self.worker.start()
        self.mark_dirty()

    def request_stop(self):
        self.stop.set()
        self.model.state = "stopping"
        for code in list(self.popups):
            self.dismiss(code)
        self.mark_dirty()

    def sound(self):
        try:
            if os.name == "nt":
                import winsound
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
            else:
                self.window.bell()
        except (RuntimeError, OSError):
            self.model.status = "声音播放失败，请检查音量或音频设备。"

    def dismiss(self, code):
        entry = self.popups.pop(code, None)
        if entry:
            entry[0].after_cancel(entry[1]); entry[0].destroy()

    def notify(self, event):
        code = event["code"]
        self.dismiss(code)
        self.sound()
        if event["kind"] == "失效":
            return
        popup = self.tk.Toplevel(self.window)
        popup.title(event["kind"]+" · "+event["name"])
        popup.configure(bg=PANEL)
        popup.attributes("-topmost", True)
        text = self.service.show_event(event)
        self.tk.Label(popup, text=text, bg=PANEL, fg=TEXT, font=(self.font, 11),
                      wraplength=520, justify="left", padx=20, pady=20).pack()
        dismiss = lambda: self.dismiss(code)
        self.ttk.Button(popup, text="知道了", command=dismiss).pack(pady=(0, 12))
        popup.protocol("WM_DELETE_WINDOW", dismiss)
        self.popups[code] = (popup, popup.after(15000, dismiss))

    def select(self, _=None):
        selected = self.table.selection()
        if selected:
            self.selected = selected[0]
            self.render_detail()

    def select_alert(self, _=None):
        selected = self.alerts.selection()
        if selected:
            index = int(selected[0])
            if index < len(self.model.events):
                self.selected = self.model.events[index]["code"]
                self.render_detail()

    def render_detail(self):
        row = self.model.quotes.get(self.selected)
        if not row:
            self.detail_title.set("选择一只股票")
            self.detail_metrics.set("启动后开始采集走势")
            self.detail_reason.set("")
            self.draw_chart()
            return
        gain = (row["price"]/row["prev"]-1)*100
        self.detail_title.set(f"{row['name']}  {row['code']}")
        self.detail_metrics.set(f"¥{row['price']:.2f}   {gain:+.2f}%   距参考涨停 {number(row.get('distance'))}%\n"
                                f"成交均价 {number(row.get('vwap'))}  回落 {number(row.get('pullback'))}%\n"
                                f"接收 {market_time(row['ts']):%H:%M:%S} · 行情时间为批次口径")
        event = self.model.latest.get(row["code"])
        reason = f"当前阶段：{self.model.stage(row, time.time())}"
        if event:
            reason += f"\n{market_time(event['ts']):%H:%M:%S} {event['kind']}：{event['reason']}"
            if event.get("low"):
                reason += f"\n承接低点参考 {event['low']:.2f}"
            evidence = event.get("metrics", {})
            if evidence.get("confirmed_updates"):
                reason += f"\n触发时确认 {evidence['confirmed_updates']}次 · 放量 {number(evidence.get('volume_ratio'), 1, '倍')}"
        position = row.get("position")
        context = row.get("context", {})
        reason += "\n"+context.get("pattern", "首板/连板待核实")
        for sector in context.get("sectors", [])[:3]:
            reason += f"\n{sector['name']} {number(sector.get('gain'), 2, '%', True)} · 相对强度 {number(sector.get('relative_gain'), 2, '个百分点', True)}"
        if not context.get("sectors"):
            reason += "\n行业归属：未获取或映射尚未完成"
        if position:
            reason += f"\n持仓 {position['shares']}股 · 成本 {position['cost']:.2f} · 浮盈亏 {(row['price']/position['cost']-1)*100:+.2f}%"
            reason += "\n"+position.get("status", "等待行情")
        self.detail_reason.set(reason)
        self.draw_chart()

    def draw_chart(self):
        canvas = self.chart
        canvas.delete("all")
        w, h = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 180)
        points = list(self.model.histories.get(self.selected, []))
        if len(points) < 2:
            canvas.create_text(w/2, h/2, text="等待已采集的分时数据", fill=MUTED)
            return
        top, bottom, left, right = 30, h*0.68, 48, w-15
        prices = [r["price"] for r in points]
        vwaps = [r.get("vwap") for r in points if r.get("vwap")]
        lo, hi = min(prices+vwaps), max(prices+vwaps)
        pad = max((hi-lo)*0.12, hi*0.001)
        lo, hi = lo-pad, hi+pad
        start, end = points[0]["ts"], points[-1]["ts"]
        x = lambda ts: left+(ts-start)/max(1, end-start)*(right-left)
        y = lambda p: bottom-(p-lo)/(hi-lo)*(bottom-top)
        for i in range(4):
            value = lo+(hi-lo)*i/3
            yy = y(value)
            canvas.create_line(left, yy, right, yy, fill="#2b3a50")
            canvas.create_text(left-5, yy, text=f"{value:.2f}", anchor="e", fill=MUTED, font=("Arial", 9))
        line = [v for r in points for v in (x(r["ts"]), y(r["price"]))]
        canvas.create_line(*line, fill=BLUE, width=2)
        average = [v for r in points if r.get("vwap") for v in (x(r["ts"]), y(r["vwap"]))]
        if len(average) >= 4:
            canvas.create_line(*average, fill=AMBER, width=1)
        deltas = [max(0, b["turnover"]-a["turnover"])/max(0.1, b["ts"]-a["ts"])
                  if b["ts"]-a["ts"] <= self.config["max_observation_gap_seconds"] else 0
                  for a, b in zip(points, points[1:])]
        scale = max(deltas, default=1) or 1
        for r, rate in zip(points[1:], deltas):
            xx = x(r["ts"])
            canvas.create_line(xx, h-25, xx, h-25-rate/scale*(h*0.2), fill="#52799f", width=3)
        canvas.create_text(left, 12, text="已采集价格 / 成交均价 · 下方为成交额速率", anchor="w", fill=MUTED, font=(self.font, 9))
        canvas.create_text(left, h-10, text=f"{market_time(start):%H:%M:%S}", anchor="w", fill=MUTED)
        canvas.create_text(right, h-10, text=f"{market_time(end):%H:%M:%S}", anchor="e", fill=MUTED)

    def refresh(self):
        self.banner.set(STATES.get(self.model.state, self.model.state))
        if self.model.state == "retrying" and self.model.last_connection:
            self.banner.set(self.model.last_connection["stage"]+"重试")
        self.state_label.configure(foreground=RED if self.model.state == "error" else AMBER if self.model.state in ("retrying", "stale", "paused") else GREEN if self.model.state in ("running", "demo") else MUTED)
        alive = bool(self.worker and self.worker.is_alive())
        self.start_button.configure(state="disabled" if alive else "normal")
        self.stop_button.configure(state="normal" if alive and not self.stop.is_set() else "disabled")
        stats = self.model.stats
        received = stats.get("received")
        self.cards["更新"].set(f"{market_time(received):%H:%M:%S}" if received else "—")
        self.cards["股票池"].set(str(stats.get("universe", "—")))
        self.cards["观察池"].set(str(stats.get("watch", "—")))
        self.cards["候选"].set(str(self.model.counts["入场候选"]))
        self.cards["请求"].set(number(stats.get("elapsed"), 2, "秒"))
        self.status.set(self.model.status)
        context = self.model.context if self.config["context_enabled"] else {"enabled": False}
        if not context.get("enabled", True):
            self.market.set("市场背景已关闭")
        else:
            background = []
            now = time.time()
            for code in ("000001.SH", "399001.SZ"):
                index = context.get("indices", {}).get(code)
                if index and -5 <= now-index["timestamp"]/1000 <= self.config["context_interval_seconds"]*2:
                    background.append(f"{index['name']} {number(index['gain'], 2, '%', True)}")
            market = context.get("market")
            if market and -5 <= now-market["timestamp"]/1000 <= self.config["context_interval_seconds"]*2:
                background.append(f"全市场涨停 {market.get('limit_up_stock_count', '—')}家 · 打开 {market.get('open_stock_count', '—')}家 · 封板率 {number(market.get('seal_rate')*100 if isinstance(market.get('seal_rate'), (int, float)) else None, 1, '%')}")
            background.append("行业映射 "+context.get("mapping", "等待数据"))
            if context.get("errors"):
                background.append("部分附加数据未获取")
            self.market.set("  |  ".join(background))
        rows = self.model.rows(self.filter.get(), self.search.get().strip())
        wanted = {row["code"] for row, _ in rows}
        for iid in self.table.get_children():
            if iid not in wanted:
                self.table.delete(iid)
        for i, (row, stage) in enumerate(rows):
            values = (row["name"], row["code"], stage, number(row["price"]),
                      number((row["price"]/row["prev"]-1)*100, 2, "%", True), number(row.get("speed"), 2, "%", True),
                      number(row.get("volume_ratio"), 1, "×"), number(row.get("distance"), 2, "%"))
            code = row["code"]
            if self.table.exists(code):
                self.table.item(code, values=values, tags=(stage,)); self.table.move(code, "", i)
            else:
                self.table.insert("", i, iid=code, values=values, tags=(stage,))
        self.alerts.delete(*self.alerts.get_children())
        for i, event in enumerate(self.model.events):
            self.alerts.insert("", "end", iid=str(i), values=(f"{market_time(event['ts']):%H:%M:%S}", event["kind"], event["name"], number(event["price"])))
        if not self.selected and rows:
            self.selected = rows[0][0]["code"]
            self.table.selection_set(self.selected)
        self.render_detail()
        self.dirty = False

    def pump(self):
        processed = 0
        try:
            while processed < 300:
                kind, value = self.messages.get_nowait()
                if kind == "event":
                    value = dict(value)
                    if self.model.mode != "demo" and value["kind"] == "入场候选" and (self.stop.is_set() or time.time()-value["ts"] > self.config["signal_expiry_seconds"]):
                        value.update(kind="失效", reason="监控已停止或提醒到达时已过期")
                    if not self.stop.is_set():
                        self.notify(value)
                self.model.ingest(kind, value)
                self.mark_dirty()
                processed += 1
        except queue.Empty:
            pass
        if self.dirty or int(time.time()) != getattr(self, "last_second", None):
            self.refresh()
            self.last_second = int(time.time())
        self.pump_id = self.window.after(200, self.pump)

    def position_dialog(self):
        from tkinter import messagebox, simpledialog
        try:
            code = simpledialog.askstring("持仓", "完整代码，例如600000.SH：", parent=self.window)
            if not code:
                return
            code = code.strip().upper()
            if not re.fullmatch(r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ)", code):
                raise ValueError()
            if messagebox.askyesno("持仓", "录入或更新持仓？选择“否”可移除持仓记录。"):
                cost = simpledialog.askfloat("成本", "实际买入均价：", minvalue=0.001)
                if cost is None: return
                shares = simpledialog.askinteger("数量", "实际持仓股数：", minvalue=1)
                if shares is None: return
                day = simpledialog.askstring("日期", "买入日期 YYYY-MM-DD：", initialvalue=str(datetime.now(CN).date()))
                if not day: return
                day = str(datetime.strptime(day, "%Y-%m-%d").date())
                if not math.isfinite(cost) or day > str(datetime.now(CN).date()): raise ValueError()
                position = {"cost": cost, "shares": shares, "buy_date": day}
            elif messagebox.askyesno("确认", f"移除 {code} 的本地持仓记录？"):
                position = None
            else:
                return
            self.service.Positions().set(code, position)
            if code in self.model.quotes:
                self.model.quotes[code]["position"] = position
            self.model.status = f"已更新 {code} 的持仓记录"
            self.mark_dirty()
        except (ValueError, KeyError, OSError):
            messagebox.showerror("输入错误", "请检查代码、日期、数量、成本和文件权限。")

    def help(self):
        from tkinter import messagebox
        messagebox.showinfo("使用说明", "颜色：红色为入场候选，黄色为承接观察，蓝色为实际持仓。\n"
                            "近60秒涨速只使用已采集样本；量能倍数为当前成交速率相对回落期速率。\n"
                            "走势图只展示程序已采集的数据，参考涨停价不是权威交易价格。\n"
                            "先演示再启动实时监控；笔记本须保持联网、不休眠。\n"
                            "参数仍属实验，信号不等于成交；T+1、费用和流动性需另行评估。")

    def connection_details(self):
        from tkinter import messagebox
        labels = dict(calendar="交易日历", previous="昨日涨停池", stats="涨停市场统计", catalog="行业目录",
                      indices="指数行情", members="行业成分", cache="本地缓存")
        errors = self.model.context.get("errors", {})
        text = "主监控："+STATES.get(self.model.state, self.model.state)
        if self.model.last_error:
            text += "\n停止原因："+self.model.last_error
        issue = self.model.last_connection
        if issue:
            text += f"\n最近异常 {issue['time']}\n{issue['stage']}：{issue['message']}"
            if issue["wait"]:
                text += f"\n当次退避 {issue['wait']}秒"
        text += "\n行业映射："+self.model.context.get("mapping", "等待数据")
        text += "\n\n"+("\n".join(labels.get(k,k)+"："+v for k,v in errors.items()) if errors else "尚未记录附加接口错误；未显示数值也可能是尚未请求或数据已过期。")
        messagebox.showinfo("数据连接状态", text)

    def close(self):
        self.stop.set(); self.key.set("")
        for code in list(self.popups):
            self.dismiss(code)
        self.window.after_cancel(self.pump_id)
        self.window.destroy()


def run(config, service):
    import tkinter as tk
    window = tk.Tk()
    Dashboard(window, config, service)
    window.mainloop()
