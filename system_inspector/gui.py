#!/usr/bin/env python3
"""System Inspector - a desktop GUI that shows detailed information about your Linux system.

Run:  python3 -m system_inspector      (or the `system-inspector` command once installed)
If Tkinter is missing:  sudo apt install python3-tk   (Debian/Ubuntu)
                        sudo dnf install python3-tkinter   (Fedora)
                        sudo pacman -S tk   (Arch)
"""
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import threading
from collections import deque
import time
import tkinter as tk
from tkinter import filedialog, ttk

from . import collectors as sysinfo

FONT = "Sans"

THEMES = {
    "dark": dict(bg="#1b1c23", panel="#252733", side="#14151b", fg="#e8e8ee", muted="#9b9db0",
                 accent="#4f8cff", hover="#2d3040", alt="#2b2e3c", trough="#383b4d",
                 warn="#f0a43a", crit="#ef5b5b"),
    "light": dict(bg="#f1f2f6", panel="#ffffff", side="#e6e8ef", fg="#1d1f27", muted="#6b6f80",
                  accent="#2f6bff", hover="#d9dce7", alt="#f6f7fb", trough="#dfe2ec",
                  warn="#e08a00", crit="#d93636"),
}

PAGES = [("overview", "Overview"), ("system", "System"), ("cpu", "CPU"), ("memory", "Memory"),
         ("storage", "Storage"), ("network", "Network"), ("users", "Users"),
         ("hardware", "Hardware"), ("processes", "Processes"), ("software", "Software")]
TITLES = dict(PAGES)


def to_blocks(data):
    """Split a section's data into display blocks: (title, kind, payload)."""
    scalars, blocks = {}, []
    for k, v in data.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            blocks.append((k, "table", v))
        elif isinstance(v, list):
            blocks.append((k, "list", v))
        elif isinstance(v, dict):
            blocks.append((k, "kv", v))
        else:
            scalars[k] = v
    return ([("", "kv", scalars)] if scalars else []) + blocks


class Spark(tk.Canvas):
    """Tiny live area-chart used on the dashboard cards."""
    N = 60

    def __init__(self, parent, theme):
        super().__init__(parent, height=40, highlightthickness=0, bg=theme["panel"])

    def draw(self, values, theme, fixed_max=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or len(values) < 2:
            return
        top = fixed_max or max(max(values), 1)
        step, off = w / (self.N - 1), self.N - len(values)
        pts = []
        for i, v in enumerate(values):
            pts += [(off + i) * step, h - 3 - min(v / top, 1) * (h - 8)]
        self.create_polygon([pts[0], h] + pts + [pts[-2], h], fill=theme["trough"], outline="")
        self.create_line(pts, fill=theme["accent"], width=2, smooth=True)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("System Inspector")
        self.geometry("1180x760")
        self.minsize(900, 560)
        self.theme = "dark"
        self.cache, self.requested = {}, set()
        self.current = "overview"
        self.live_job = None
        self.prev_cpu = None
        self.prev_net = None
        self.hist = {k: deque(maxlen=Spark.N) for k in ("cpu", "mem", "net")}
        self.sparks = {}
        self.filter = tk.StringVar()
        self.auto = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Ready")
        self.style = ttk.Style(self)
        self.apply_theme()
        self.build_ui()
        self.filter.trace_add("write", lambda *_: self.after(150, self.rerender))
        self.show("overview")
        self.after(5000, self.auto_tick)

    # ---------- theme ----------
    def apply_theme(self):
        t, s = THEMES[self.theme], self.style
        s.theme_use("clam")
        self.configure(bg=t["bg"])
        s.configure(".", background=t["bg"], foreground=t["fg"], font=(FONT, 10), bordercolor=t["bg"])
        s.configure("TFrame", background=t["bg"])
        s.configure("Side.TFrame", background=t["side"])
        s.configure("Card.TFrame", background=t["panel"])
        s.configure("TLabel", background=t["bg"], foreground=t["fg"])
        s.configure("Card.TLabel", background=t["panel"], foreground=t["fg"])
        s.configure("Muted.Card.TLabel", background=t["panel"], foreground=t["muted"])
        s.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
        s.configure("Side.TLabel", background=t["side"], foreground=t["muted"], font=(FONT, 8, "bold"))
        s.configure("Title.Card.TLabel", background=t["panel"], font=(FONT, 11, "bold"))
        s.configure("Big.Card.TLabel", background=t["panel"], font=(FONT, 22, "bold"))
        s.configure("H1.Card.TLabel", background=t["panel"], font=(FONT, 15, "bold"))
        s.configure("Card.TCheckbutton", background=t["panel"], foreground=t["fg"])
        s.map("Card.TCheckbutton", background=[("active", t["panel"])])
        s.configure("TButton", background=t["alt"], foreground=t["fg"], borderwidth=0, padding=(12, 6))
        s.map("TButton", background=[("active", t["hover"])])
        s.configure("Nav.TButton", background=t["side"], foreground=t["fg"], anchor="w",
                    padding=(20, 11), relief="flat", borderwidth=0)
        s.map("Nav.TButton", background=[("active", t["hover"])])
        s.configure("NavActive.TButton", background=t["accent"], foreground="white", anchor="w",
                    padding=(20, 11), relief="flat", borderwidth=0)
        s.map("NavActive.TButton", background=[("active", t["accent"])])
        s.configure("TEntry", fieldbackground=t["alt"], foreground=t["fg"], bordercolor=t["alt"],
                    insertcolor=t["fg"], padding=5, lightcolor=t["alt"], darkcolor=t["alt"])
        s.configure("Treeview", background=t["panel"], fieldbackground=t["panel"], foreground=t["fg"],
                    rowheight=26, borderwidth=0, bordercolor=t["panel"],
                    lightcolor=t["panel"], darkcolor=t["panel"])
        s.configure("Treeview.Heading", background=t["side"], foreground=t["fg"], relief="flat",
                    font=(FONT, 9, "bold"), padding=(6, 5))
        s.map("Treeview.Heading", background=[("active", t["side"])])
        s.map("Treeview", background=[("selected", t["accent"])], foreground=[("selected", "white")])
        for o in ("Vertical", "Horizontal"):
            s.configure(f"{o}.TScrollbar", background=t["alt"], troughcolor=t["panel"], borderwidth=0,
                        arrowcolor=t["muted"], lightcolor=t["alt"], darkcolor=t["alt"],
                        bordercolor=t["panel"])
            s.map(f"{o}.TScrollbar", background=[("active", t["hover"]), ("!active", t["alt"])])
        for name, col in (("Ok", t["accent"]), ("Warn", t["warn"]), ("Crit", t["crit"])):
            s.configure(f"{name}.Horizontal.TProgressbar", troughcolor=t["trough"], background=col,
                        borderwidth=0, thickness=10, lightcolor=col, darkcolor=col)

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.apply_theme()
        self.canvas.configure(bg=THEMES[self.theme]["bg"])
        self.show(self.current)

    # ---------- layout ----------
    def build_ui(self):
        header = ttk.Frame(self, style="Card.TFrame", padding=(18, 12))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Card.TFrame")
        left.pack(side="left")
        ttk.Label(left, text="System Inspector", style="H1.Card.TLabel").pack(anchor="w")
        self.subtitle = ttk.Label(left, text="Loading…", style="Muted.Card.TLabel")
        self.subtitle.pack(anchor="w")
        right = ttk.Frame(header, style="Card.TFrame")
        right.pack(side="right")
        ttk.Label(right, text="Filter", style="Muted.Card.TLabel").pack(side="left", padx=(0, 6))
        ttk.Entry(right, textvariable=self.filter, width=22).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(right, text="Auto-refresh", variable=self.auto,
                        style="Card.TCheckbutton").pack(side="left", padx=(0, 10))
        ttk.Button(right, text="Refresh", command=self.refresh).pack(side="left", padx=3)
        ttk.Button(right, text="Export", command=self.export).pack(side="left", padx=3)
        ttk.Button(right, text="Theme", command=self.toggle_theme).pack(side="left", padx=3)

        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)
        self.side = ttk.Frame(main, style="Side.TFrame", width=190)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        ttk.Label(self.side, text="  BROWSE", style="Side.TLabel").pack(anchor="w", pady=(16, 6), padx=8)
        self.nav = {}
        for key, label in PAGES:
            b = ttk.Button(self.side, text=label, style="Nav.TButton", command=lambda k=key: self.show(k))
            b.pack(fill="x")
            self.nav[key] = b

        wrap = ttk.Frame(main)
        wrap.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg=THEMES[self.theme]["bg"])
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas)
        self.win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.win, width=e.width))
        self.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 * (e.delta // 40 or 1), "units"))

        bar = ttk.Frame(self, style="Side.TFrame", padding=(14, 4))
        bar.pack(fill="x", side="bottom")
        who = "root" if os.geteuid() == 0 else "a normal user (run with sudo for more detail)"
        ttk.Label(bar, textvariable=self.status, style="Side.TLabel").pack(side="left")
        ttk.Label(bar, text=f"Running as {who}", style="Side.TLabel").pack(side="right")

    # ---------- navigation / loading ----------
    def show(self, key):
        self.current = key
        for k, b in self.nav.items():
            b.configure(style="NavActive.TButton" if k == key else "Nav.TButton")
        if self.live_job:
            self.after_cancel(self.live_job)
            self.live_job = None
        for w in self.body.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)
        if key == "overview":
            self.build_overview()
            self.tick()
        elif key in self.cache:
            self.render_section(key)
        else:
            ttk.Label(self.body, text="Loading…", style="Muted.TLabel").pack(padx=24, pady=24, anchor="w")
            self.load(key)

    def load(self, key):
        if key in self.requested and key not in self.cache and key != self.current:
            return
        self.requested.add(key)
        self.status.set(f"Reading {TITLES[key]}…")

        def work():
            try:
                data = sysinfo.SECTIONS[key]()
            except Exception as e:  # keep the UI alive whatever happens
                data = {"Error": str(e)}
            self.after(0, lambda: self.loaded(key, data))
        threading.Thread(target=work, daemon=True).start()

    def loaded(self, key, data):
        self.cache[key] = data
        self.status.set(f"Updated {TITLES[key]} at {time.strftime('%H:%M:%S')}")
        if key == "system":
            self.subtitle.configure(text=f"{data.get('Hostname', '')}  •  {data.get('OS', '')}  •  "
                                         f"kernel {data.get('Kernel', '')}")
        if self.current == key:
            self.render_section(key)
        elif self.current == "overview":
            self.fill_facts()

    def refresh(self):
        if self.current == "overview":
            for k in ("system", "cpu", "memory"):
                self.cache.pop(k, None)
                self.requested.discard(k)
            self.fill_facts()
        else:
            self.load(self.current)

    def auto_tick(self):
        if self.auto.get() and self.current != "overview":
            self.load(self.current)
        self.after(5000, self.auto_tick)

    def rerender(self):
        if self.current != "overview" and self.current in self.cache:
            self.render_section(self.current)

    # ---------- section pages ----------
    def render_section(self, key):
        for w in self.body.winfo_children():
            w.destroy()
        ttk.Label(self.body, text=TITLES[key], font=(FONT, 18, "bold")).pack(anchor="w", padx=22, pady=(18, 2))
        flt = self.filter.get().strip().lower()
        shown = 0
        for title, kind, payload in to_blocks(self.cache[key]):
            if self.make_block(self.body, title or TITLES[key], kind, payload, flt):
                shown += 1
        if not shown:
            msg = "Nothing matches your filter." if flt else "No information available on this system."
            ttk.Label(self.body, text=msg, style="Muted.TLabel").pack(anchor="w", padx=24, pady=16)

    def make_block(self, parent, title, kind, data, flt=""):
        if kind == "kv":
            cols, rows = ["Property", "Value"], [[str(k), str(v)] for k, v in data.items()]
        elif kind == "table":
            cols = list(dict.fromkeys(k for r in data for k in r))
            rows = [[str(r.get(c, "")) for c in cols] for r in data]
        else:
            cols, rows = ["Value"], [[str(x)] for x in data]
        if flt:
            rows = [r for r in rows if flt in " ".join(r).lower()]
        if not rows:
            return None
        card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 12))
        card.pack(fill="x", padx=18, pady=7)
        ttk.Label(card, text=f"{title}  ({len(rows)})" if kind != "kv" else title,
                  style="Title.Card.TLabel").pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(card, columns=cols, show="headings", height=min(len(rows), 20),
                            selectmode="browse")
        xs = ttk.Scrollbar(card, orient="horizontal", command=tree.xview)
        tree.configure(xscrollcommand=xs.set)
        for i, c in enumerate(cols):
            width = min(max(len(c), *(len(r[i]) for r in rows)) * 8 + 24, 300)
            if kind == "kv" and i == 0:
                width = 240
            tree.heading(c, text=c.upper() if kind == "table" else c, anchor="w")
            tree.column(c, width=width, minwidth=60, stretch=(i == len(cols) - 1))
        tree.tag_configure("odd", background=THEMES[self.theme]["alt"])
        for n, r in enumerate(rows):
            tree.insert("", "end", values=r, tags=("odd",) if n % 2 else ())
        tree.pack(fill="x")
        if kind == "table":
            xs.pack(fill="x")
        tree.bind("<Double-1>", lambda e, t=tree: self.copy_row(t))
        return card

    def copy_row(self, tree):
        sel = tree.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\t".join(str(v) for v in tree.item(sel[0], "values")))
            self.status.set("Row copied to clipboard")

    # ---------- overview dashboard ----------
    def build_overview(self):
        ttk.Label(self.body, text="Overview", font=(FONT, 18, "bold")).pack(anchor="w", padx=22, pady=(18, 2))
        grid = ttk.Frame(self.body)
        grid.pack(fill="x", padx=12, pady=6)
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="c")
        self.cards = {}
        specs = [("cpu", "CPU usage"), ("mem", "Memory"), ("swap", "Swap"),
                 ("disk", "Disk  /"), ("net", "Network"), ("up", "Uptime")]
        for i, (k, title) in enumerate(specs):
            card = ttk.Frame(grid, style="Card.TFrame", padding=16)
            card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
            ttk.Label(card, text=title, style="Muted.Card.TLabel").pack(anchor="w")
            big = ttk.Label(card, text="–", style="Big.Card.TLabel")
            big.pack(anchor="w", pady=(4, 6))
            bar = None
            if k in ("cpu", "mem", "swap", "disk"):
                bar = ttk.Progressbar(card, maximum=100, style="Ok.Horizontal.TProgressbar")
                bar.pack(fill="x")
            sub = ttk.Label(card, text=" ", style="Muted.Card.TLabel")
            sub.pack(anchor="w", pady=(6, 0))
            if k in self.hist:
                sp = Spark(card, THEMES[self.theme])
                sp.pack(fill="x", pady=(8, 0))
                self.sparks[k] = sp
            self.cards[k] = (big, bar, sub)
        self.facts = ttk.Frame(self.body)
        self.facts.pack(fill="x", padx=12, pady=(4, 20))
        self.fill_facts()

    def fill_facts(self):
        if self.current != "overview":
            return
        for w in self.facts.winfo_children():
            w.destroy()
        missing = [k for k in ("system", "cpu", "memory") if k not in self.cache]
        for k in missing:
            if k not in self.requested:
                self.load_quiet(k)
        s, c, m = (self.cache.get(k, {}) for k in ("system", "cpu", "memory"))
        facts = {
            "Hostname": s.get("Hostname"), "Operating system": s.get("OS"), "Kernel": s.get("Kernel"),
            "Architecture": s.get("Architecture"), "Processor": c.get("Model"),
            "Cores (physical / logical)": f"{c.get('Physical cores')} / {c.get('Logical CPUs')}" if c else None,
            "Installed RAM": m.get("RAM total"), "Desktop": s.get("Desktop"),
            "Virtualization": s.get("Virtualization"), "Booted": s.get("Boot time"),
        }
        facts = {k: v for k, v in facts.items() if v}
        if facts:
            self.make_block(self.facts, "Quick facts", "kv", facts)

    def load_quiet(self, key):
        self.requested.add(key)

        def work():
            try:
                data = sysinfo.SECTIONS[key]()
            except Exception as e:
                data = {"Error": str(e)}
            self.after(0, lambda: self.loaded(key, data))
        threading.Thread(target=work, daemon=True).start()

    def set_card(self, key, big, sub="", pct=None):
        lbl, bar, subl = self.cards[key]
        lbl.configure(text=big)
        subl.configure(text=sub or " ")
        if bar is not None and pct is not None:
            bar["value"] = pct
            bar.configure(style=("Crit" if pct >= 90 else "Warn" if pct >= 70 else "Ok") + ".Horizontal.TProgressbar")

    def tick(self):
        if self.current != "overview":
            return
        try:
            # CPU
            v = list(map(int, sysinfo.read("/proc/stat").splitlines()[0].split()[1:]))
            idle, total = v[3] + (v[4] if len(v) > 4 else 0), sum(v)
            if self.prev_cpu:
                di, dt = idle - self.prev_cpu[0], total - self.prev_cpu[1]
                pct = 100 * (1 - di / dt) if dt else 0
                la = os.getloadavg()
                self.set_card("cpu", f"{pct:.0f}%", f"Load {la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}", pct)
                self.hist["cpu"].append(pct)
            self.prev_cpu = (idle, total)
            # Memory / swap
            mi = {}
            for line in sysinfo.read("/proc/meminfo").splitlines():
                k, val = line.split(":", 1)
                mi[k] = int(val.split()[0]) * 1024
            tot, av = mi["MemTotal"], mi["MemAvailable"]
            used = tot - av
            self.set_card("mem", f"{used * 100 // tot}%", f"{sysinfo.human(used)} of {sysinfo.human(tot)}",
                          used * 100 / tot)
            self.hist["mem"].append(used * 100 / tot)
            st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
            if st:
                self.set_card("swap", f"{(st - sf) * 100 // st}%",
                              f"{sysinfo.human(st - sf)} of {sysinfo.human(st)}", (st - sf) * 100 / st)
            else:
                self.set_card("swap", "None", "No swap configured", 0)
            # Disk
            du = shutil.disk_usage("/")
            self.set_card("disk", f"{du.used * 100 // du.total}%",
                          f"{sysinfo.human(du.free)} free of {sysinfo.human(du.total)}", du.used * 100 / du.total)
            # Network
            rx = tx = 0
            for line in sysinfo.read("/proc/net/dev").splitlines()[2:]:
                name, data = line.split(":", 1)
                if name.strip() != "lo":
                    f = data.split()
                    rx, tx = rx + int(f[0]), tx + int(f[8])
            now = time.time()
            if self.prev_net:
                dt = max(now - self.prev_net[0], 0.001)
                self.hist["net"].append((rx - self.prev_net[1]) / dt)
                self.set_card("net", f"↓ {sysinfo.human((rx - self.prev_net[1]) / dt)}/s",
                              f"↑ {sysinfo.human((tx - self.prev_net[2]) / dt)}/s    total ↓ {sysinfo.human(rx)}")
            self.prev_net = (now, rx, tx)
            for k, sp in self.sparks.items():
                sp.draw(list(self.hist[k]), THEMES[self.theme], 100 if k != "net" else None)
            # Uptime
            up = float(sysinfo.read("/proc/uptime", "0 0").split()[0])
            self.set_card("up", sysinfo.duration(up), f"Since {time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() - up))}")
        except Exception as e:
            self.status.set(f"Live update error: {e}")
        self.live_job = self.after(1000, self.tick)

    # ---------- export ----------
    def export(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile=f"sysinfo-{socket.gethostname()}.json",
            filetypes=[("JSON", "*.json"), ("Text", "*.txt")])
        if not path:
            return
        self.status.set("Exporting…")

        def work():
            try:
                data = {n: f() for n, f in sysinfo.SECTIONS.items()}
                if path.endswith(".txt"):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        for n, d in data.items():
                            print(f"\n=== {n.upper()} ===")
                            sysinfo.render(d)
                    text = buf.getvalue()
                else:
                    text = json.dumps(data, indent=2)
                with open(path, "w") as fh:
                    fh.write(text)
                msg = f"Exported to {path}"
            except Exception as e:
                msg = f"Export failed: {e}"
            self.after(0, lambda: self.status.set(msg))
        threading.Thread(target=work, daemon=True).start()


def main():
    if not sys.platform.startswith("linux"):
        print("System Inspector is built for Linux.")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
