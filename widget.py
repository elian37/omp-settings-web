# -*- coding: utf-8 -*-
"""omp quota desktop widget: borderless, desktop-level (below all app windows), dark; data from `omp usage --json`.

Run with pythonw (no console). Drag anywhere to move; position is remembered and the window is
kept fully inside the nearest monitor's work area (on start, drag, and display/resolution changes).
Right-click menu: Refresh / Force refresh / Open web panel / Quit. Stdlib only.

`QuotaView` is the quota list itself (data + drawing) and can be embedded in any Tk parent;
desktop-widgets' Model Quota tab loads this file and reuses it, so quota logic lives only here.
"""
import ctypes
import json
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / "widget-state.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REFRESH_MS = 60_000
TICK_MS = 30_000
GUARD_MS = 2_000  # re-clamp on resolution/monitor/taskbar changes (Tk calls from a wndproc crash)
WEB_URL = "http://127.0.0.1:8788"

BG = "#16181d"
EDGE = "#2a2e37"
FG = "#d7dae0"
DIM = "#7d8590"
ACCENT = "#9fc3ff"
TRACK = "#262a33"
STATUS_COLOR = {"ok": "#3fb950", "warning": "#d29922", "exhausted": "#f85149"}
UNKNOWN_COLOR = "#58a6ff"
FONT = "Microsoft YaHei UI"
NUM_FONT = "Segoe UI"
WIN_TAG = {"weekly": "7d", "daily": "1d", "monthly": "1mo"}


def single_instance():
    """Named mutex prevents a second instance (scheduled task vs manual start)."""
    if sys.platform != "win32":
        return True
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "omp-quota-widget")
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run_omp(args, timeout, no_ext=True, omp="omp", procs=None):
    """Run omp; `procs` (a set) tracks the child so an embedding host can kill it on close."""
    proc = subprocess.Popen([omp, *args, *(["--no-extensions"] if no_ext else [])],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    if procs is not None:
        procs.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        if procs is not None:
            procs.discard(proc)
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def fetch_usage(invalidate, omp="omp", procs=None):
    try:
        if invalidate:
            run_omp(["usage", "invalidate"], 60, omp=omp, procs=procs)
        _, out, err = run_omp(["usage", "--json"], 120, omp=omp, procs=procs)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"omp call failed: {e}"
    try:
        data = json.loads(out)
        if not isinstance(data, dict) or not isinstance(data.get("reports"), list):
            raise ValueError
    except ValueError:
        lines = err.strip().splitlines()
        return None, lines[0] if lines else "unparseable omp usage output"
    try:  # 账号策略只用于标记「停用·兜底」，取不到就不标；`omp config get` 不接受 --no-extensions
        _, out, _ = run_omp(["config", "get", "auth.accountPolicies"], 30, no_ext=False, omp=omp, procs=procs)
        pol = json.loads(out)
        data["_policies"] = pol if isinstance(pol, list) else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data["_policies"] = []
    return data, None


def is_disabled(rep, policies):
    """设置页的「停用（仅兜底）」= 该账号策略 reservePct 为 100；选择器字段须全部对上。"""
    md = rep.get("metadata") or {}
    for p in policies:
        if not isinstance(p, dict) or p.get("provider") != rep.get("provider") or p.get("reservePct") != 100:
            continue
        acct = p.get("account") or {}
        keys = [k for k in ("email", "accountId", "projectId", "orgId") if k in acct]
        if keys and all(str(acct[k]).casefold() == str(md.get(k) or "").casefold() for k in keys):
            return True
    return False


def win_tag(w):
    wid = w.get("id") or ""
    if wid:
        return WIN_TAG.get(wid, wid)
    ms = w.get("durationMs")
    if ms:
        h = ms / 3_600_000
        return f"{h:g}h" if h < 24 else f"{h / 24:g}d"
    return ""


def provider_rows(rep):
    """Dedup shared windows; append a window tag when identical labels repeat."""
    limits, seen = [], set()
    for lim in rep.get("limits") or []:
        w = lim.get("window") or {}
        key = f"{(lim.get('scope') or {}).get('sharedGroup') or lim.get('id') or ''}|{w.get('id') or ''}"
        if key not in seen:
            seen.add(key)
            limits.append(lim)
    labels = [lim.get("label") or "" for lim in limits]
    repeated = len(set(labels)) < len(labels)
    rows = []
    for lim in limits:
        w, a = lim.get("window") or {}, lim.get("amount") or {}
        used = a.get("usedFraction")
        if used is None and a.get("limit"):
            used = (a.get("used") or 0) / a["limit"]
        remain = a.get("remainingFraction")
        frac = remain if remain is not None else (None if used is None else 1.0 - used)
        tag = win_tag(w)
        label = (lim.get("label") or "").replace(" (shared)", "")
        rows.append({
            "name": f"{label} {tag}".strip() if repeated else (tag or label),
            "frac": frac,  # remaining fraction: full bar = quota healthy
            "resets": w.get("resetsAt"),
        })
    return rows


def visible_quota(reports):
    """(report, rows) pairs; rows without a numeric remaining share and providers left empty are hidden."""
    shown = []
    for rep in reports or []:
        rows = [r for r in provider_rows(rep) if r["frac"] is not None]
        if rows:
            shown.append((rep, rows))
    return shown


def load_state():
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def ordered(reports, order):
    """Providers named in `order` come first, in that order; the rest keep omp's order."""
    rank = {p: i for i, p in enumerate(order)}
    return sorted(reports, key=lambda x: rank.get(x[0].get("provider"), len(rank)))


def time_left(ts):
    if not ts:
        return ""
    s = ts / 1000 - time.time()
    if s <= 60:
        return "due"
    d, m = divmod(int(s // 60), 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}d{h}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def virtual_screen(root):
    try:
        u = ctypes.windll.user32
        x, y = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        return x, y, x + u.GetSystemMetrics(78), y + u.GetSystemMetrics(79)
    except (AttributeError, OSError):
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def work_area(x, y):
    """Work area (taskbar excluded) of the monitor nearest to (x, y); None if unavailable."""
    try:
        u = ctypes.windll.user32
        u.MonitorFromPoint.restype = wintypes.HMONITOR
        u.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
        u.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if not u.GetMonitorInfoW(u.MonitorFromPoint(wintypes.POINT(x, y), 2), ctypes.byref(mi)):  # NEAREST
            return None
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    except (AttributeError, OSError):
        return None


class QuotaView:
    """The quota list: background refresh, provider headers, limit rows, provider reordering.

    Embeddable in any Tk parent. Callbacks: on_order(names) persists a new provider order,
    on_render() runs after each redraw (hosts re-fit their window or scroll region).
    close() cancels timers and kills a running omp call."""

    def __init__(self, parent, order=(), on_order=None, on_render=None, omp="omp", quit_cb=None):
        self.frame = tk.Frame(parent, bg=BG)
        self.k = parent.winfo_fpixels("1i") / 96  # 像素尺寸随 DPI 缩放
        self.order = [p for p in order if isinstance(p, str)]
        self.on_order, self.on_render, self.omp, self.quit_cb = on_order, on_render, omp, quit_cb
        self.pmenu = tk.Menu(self.frame, tearoff=0, bg="#1f232b", fg=FG, activebackground="#2d6cdf",
                             activeforeground="#fff", bd=0, font=(FONT, 9))
        self.shown: list[str] = []
        self.cells, self.sig = [], None
        self.data, self.err, self.fetched = None, None, None
        self.loading, self.closed = False, False
        self.result: tuple | None = None  # worker thread writes (data, err); collect() consumes
        self._jobs: dict[str, str] = {}
        self._procs: set = set()
        self.frame.after_idle(self._start)

    def px(self, n):
        return int(round(n * self.k))

    def _after(self, name, ms, fn):
        if not self.closed:
            self._jobs[name] = self.frame.after(ms, fn)

    def _start(self):
        self.render()
        self.auto_refresh()
        self._after("tick", TICK_MS, self.tick)

    def close(self):
        self.closed = True
        for job in self._jobs.values():
            self.frame.after_cancel(job)
        for proc in list(self._procs):
            if proc.poll() is None:
                proc.kill()

    def status(self):
        """(text, tone) summary for hosts with their own header."""
        if self.loading:
            return "Refreshing", "busy"
        if self.err:
            return (self.err if len(self.err) <= 48 else self.err[:47] + "…"), "error"
        if self.fetched:
            return time.strftime("Remaining · %H:%M", time.localtime(self.fetched)), "ok"
        return "", "busy"

    # ---------- data ----------
    def auto_refresh(self):
        self.refresh()
        self._after("auto", REFRESH_MS, self.auto_refresh)

    def refresh(self, invalidate=False):
        if self.loading or self.closed:
            return
        self.loading = True
        self._notify()

        def work():
            self.result = fetch_usage(invalidate, self.omp, self._procs)

        threading.Thread(target=work, daemon=True).start()
        self._after("collect", 300, self.collect)

    def collect(self):
        if self.result is None:
            self._after("collect", 300, self.collect)
            return
        (data, err), self.result = self.result, None
        self.loading = False
        if data is not None:
            self.data, self.err, self.fetched = data, None, time.time()
        else:
            self.err = err
        self.render()

    def tick(self):
        if not self.loading:
            self.render()  # countdowns only
        self._after("tick", TICK_MS, self.tick)

    def _notify(self):
        if self.on_render and not self.closed:
            self.on_render()

    # ---------- draw ----------
    def render(self):
        """Update text/bars in place while the structure is unchanged (no rebuild flicker)."""
        if self.closed:
            return
        reports = ordered(visible_quota((self.data or {}).get("reports")), self.order)
        self.shown = [r.get("provider") or "?" for r, _ in reports]
        pols = (self.data or {}).get("_policies") or []
        sig = (tuple((r.get("provider"), self.account(r), (r.get("metadata") or {}).get("planType"),
                      (r.get("resetCredits") or {}).get("availableCount"), is_disabled(r, pols),
                      tuple(x["name"] for x in rows)) for r, rows in reports),
               self.err, self.data is None)
        if sig == self.sig:
            for cells, r in zip(self.cells, (x for _, rows in reports for x in rows)):
                self.fill_row(cells, r)
            self._notify()
            return
        self.sig, self.cells = sig, []
        for w in self.frame.winfo_children():
            if w is not self.pmenu:
                w.destroy()
        if not reports:
            msg = self.err or ("Loading…" if self.data is None else "No quota data")
            tk.Label(self.frame, text=msg, font=(FONT, 9), fg=DIM, bg=BG,
                     wraplength=self.px(240), justify="left").grid(row=0, column=0, sticky="w")
        line = 0
        for i, (rep, rows) in enumerate(reports):
            self.header(rep, line, top=self.px(8) if i else self.px(2), off=is_disabled(rep, pols))
            line += 1
            for r in rows:
                self.cells.append(self.limit_row(r, line))
                line += 1
        if self.err and reports:
            tk.Label(self.frame, text=self.err, font=(FONT, 8), fg=STATUS_COLOR["exhausted"], bg=BG,
                     wraplength=self.px(260), justify="left").grid(
                row=line, column=0, columnspan=4, sticky="w", pady=(self.px(6), 0))
        self._notify()

    @staticmethod
    def account(rep):
        md = rep.get("metadata") or {}
        return md.get("email") or md.get("accountId") or ""

    def header(self, rep, line, top, off=False):
        f = tk.Frame(self.frame, bg=BG)
        f.grid(row=line, column=0, columnspan=4, sticky="we", pady=(top, self.px(2)))
        tk.Label(f, text=rep.get("provider") or "?", font=(FONT, 9, "bold"),
                 fg=DIM if off else ACCENT, bg=BG).pack(side="left")
        acct = self.account(rep)
        if acct:
            tk.Label(f, text=acct, font=(FONT, 8), fg=DIM, bg=BG).pack(side="left", padx=(self.px(6), 0))
        plan = (rep.get("metadata") or {}).get("planType")
        if plan:
            tk.Label(f, text=plan, font=(FONT, 8), fg=DIM, bg=BG).pack(side="left", padx=(self.px(6), 0))
        if off:
            tk.Label(f, text="停用·兜底", font=(FONT, 8), fg="#e3b341", bg=TRACK,
                     padx=self.px(5)).pack(side="left", padx=(self.px(6), 0))
        n = (rep.get("resetCredits") or {}).get("availableCount")
        if n:
            tk.Label(f, text=f"credit×{n}", font=(FONT, 8), fg="#e3b341", bg="#2b2616",
                     padx=self.px(5)).pack(side="right")
        prov = rep.get("provider") or "?"
        for w in (f, *f.winfo_children()):
            w.bind("<Button-3>", lambda e, p=prov: self.provider_menu(e, p))

    def provider_menu(self, e, prov):
        """Right-click on a provider header: reorder providers (persisted through on_order)."""
        i = self.shown.index(prov)
        m = self.pmenu
        m.delete(0, "end")
        m.add_command(label="Move up", command=lambda: self.move(prov, -1),
                      state="normal" if i > 0 else "disabled")
        m.add_command(label="Move down", command=lambda: self.move(prov, 1),
                      state="normal" if i < len(self.shown) - 1 else "disabled")
        m.add_command(label="Reset order", command=lambda: self.set_order([]),
                      state="normal" if self.order else "disabled")
        m.add_separator()
        m.add_command(label="Refresh", command=self.refresh)
        if self.quit_cb:
            m.add_command(label="Quit", command=self.quit_cb)
        m.tk_popup(e.x_root, e.y_root)
        return "break"  # keep the host's window-wide menu from opening too

    def move(self, prov, delta):
        names = list(self.shown)
        i = names.index(prov)
        names[i], names[i + delta] = names[i + delta], names[i]
        self.set_order(names)

    def set_order(self, names):
        self.order = names
        if self.on_order:
            self.on_order(names)
        self.render()

    def limit_row(self, r, line):
        tk.Label(self.frame, text=r["name"], font=(FONT, 9), fg=FG, bg=BG).grid(
            row=line, column=0, sticky="w", padx=(self.px(8), self.px(10)))
        w, h = self.px(110), self.px(6)
        bar = tk.Canvas(self.frame, width=w, height=h, bg=BG, highlightthickness=0)
        bar.grid(row=line, column=1)
        c = h / 2
        bar.create_line(c, c, w - c, c, width=h, capstyle="round", fill=TRACK)
        fill = bar.create_line(c, c, c, c, width=h, capstyle="round", state="hidden")
        pct = tk.Label(self.frame, font=(NUM_FONT, 9), fg=FG, bg=BG, width=4, anchor="e")
        pct.grid(row=line, column=2, padx=(self.px(6), 0))
        left = tk.Label(self.frame, font=(FONT, 8), fg=DIM, bg=BG, width=8, anchor="e")
        left.grid(row=line, column=3, sticky="e", padx=(self.px(8), 0))
        cells = (bar, fill, pct, left)
        self.fill_row(cells, r)
        return cells

    def fill_row(self, cells, r):
        bar, fill, pct, left = cells
        w, h = int(bar["width"]), int(bar["height"])
        c, frac = h / 2, r["frac"]
        end = c + (w - 2 * c) * min(1.0, max(0.0, frac))
        bar.coords(fill, c, c, max(end, c + 0.1), c)
        # Green when plenty left, red when nearly exhausted
        color = "#3fb950" if frac > 0.25 else ("#d29922" if frac > 0.05 else "#f85149")
        bar.itemconfig(fill, state="normal", fill=color)
        pct.config(text=f"{round(frac * 100)}%")
        left.config(text=time_left(r["resets"]))


class Widget:
    """Standalone shell around QuotaView: borderless desktop-level window, drag, remembered position."""

    def __init__(self):
        self.root = root = tk.Tk()
        root.title("Model Quota")
        root.overrideredirect(True)
        # Desktop-level: stay beneath every app window (re-sink after any click/focus)
        root.attributes("-alpha", 0.95)
        root.configure(bg=EDGE)
        self.k = root.winfo_fpixels("1i") / 96

        card = tk.Frame(root, bg=BG, padx=self.px(12), pady=self.px(10))
        card.pack(padx=1, pady=1)
        head = tk.Frame(card, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Model Quota", font=(FONT, 10, "bold"), fg=FG, bg=BG).pack(side="left")
        close = tk.Label(head, text="×", font=(NUM_FONT, 11), fg=DIM, bg=BG, cursor="hand2")
        close.pack(side="right", padx=(self.px(6), 0))
        close.bind("<Button-1>", lambda e: self.quit())
        again = tk.Label(head, text="⟳", font=(NUM_FONT, 10), fg=DIM, bg=BG, cursor="hand2")
        again.pack(side="right", padx=(self.px(6), 0))
        again.bind("<Button-1>", lambda e: self.view.refresh())
        self.stamp = tk.Label(head, text="", font=(FONT, 8), fg=DIM, bg=BG, width=8, anchor="e")
        self.stamp.pack(side="right")

        self.state = load_state()
        order = self.state.get("order")
        self.placed = False
        self.view = QuotaView(card, order=order if isinstance(order, list) else [],
                              on_order=lambda names: self.save_state(), on_render=self.on_render,
                              quit_cb=self.quit)
        self.view.frame.pack(fill="x", pady=(self.px(4), 0))

        self.menu = tk.Menu(root, tearoff=0, bg="#1f232b", fg=FG, activebackground="#2d6cdf",
                            activeforeground="#fff", bd=0, font=(FONT, 9))
        self.menu.add_command(label="Refresh", command=self.view.refresh)
        self.menu.add_command(label="Force refresh (re-fetch)", command=lambda: self.view.refresh(True))
        self.menu.add_command(label="Open web panel", command=lambda: webbrowser.open(WEB_URL))
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        # Child widgets share the toplevel's bindtags, so bind once on root for full-window drag
        root.bind("<ButtonPress-1>", self.drag_start)
        root.bind("<B1-Motion>", self.drag_move)
        root.bind("<ButtonRelease-1>", lambda e: self.save_state())
        root.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        root.after(50, self.round_corners)
        root.after(60, self.pin_bottom)
        root.after(GUARD_MS, self.guard)

    def px(self, n):
        return int(round(n * self.k))

    def on_render(self):
        v = self.view
        self.stamp.config(text="…" if v.loading else
                          time.strftime("%H:%M", time.localtime(v.fetched)) if v.fetched else "")
        self.root.update_idletasks()
        if not self.placed:
            self.place()
        else:
            self.keep_on_screen()  # height may have grown past the bottom edge

    # ---------- window ----------
    def round_corners(self):
        """Win11 rounded corners (DWMWA_WINDOW_CORNER_PREFERENCE=33 → DWMWCP_ROUND); no-op elsewhere."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except (AttributeError, OSError):
            pass

    def pin_bottom(self):
        """Keep the window at the bottom of the z-order. Subclass the toplevel's wndproc and
        rewrite every WM_WINDOWPOSCHANGING (click activation, drag, Tk raise) to HWND_BOTTOM,
        so the window never surfaces above other apps, not even for a frame."""
        if sys.platform != "win32":
            return
        from ctypes import wintypes
        u = ctypes.windll.user32
        lresult = ctypes.c_ssize_t
        wndproc_t = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WINDOWPOS(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("hwndInsertAfter", wintypes.HWND),
                        ("x", ctypes.c_int), ("y", ctypes.c_int), ("cx", ctypes.c_int), ("cy", ctypes.c_int),
                        ("flags", wintypes.UINT)]

        u.SetWindowLongPtrW.restype = ctypes.c_void_p
        u.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
        u.CallWindowProcW.restype = lresult
        u.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        hwnd = u.GetParent(self.root.winfo_id())
        old = []

        def proc(h, msg, wp, lp):
            if msg == 0x0046:  # WM_WINDOWPOSCHANGING
                pos = ctypes.cast(lp, ctypes.POINTER(WINDOWPOS)).contents
                pos.hwndInsertAfter = 1  # HWND_BOTTOM
                pos.flags &= ~0x0004  # clear SWP_NOZORDER
            return u.CallWindowProcW(old[0], h, msg, wp, lp)

        self._wndproc = wndproc_t(proc)  # keep a reference, or ctypes frees the thunk
        old.append(u.SetWindowLongPtrW(hwnd, -4, ctypes.cast(self._wndproc, ctypes.c_void_p)))  # GWLP_WNDPROC
        u.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # initial sink: NOSIZE|NOMOVE|NOACTIVATE

    def place(self):
        self.placed = True
        width = self.root.winfo_reqwidth()
        try:
            x, y = int(self.state["x"]), int(self.state["y"])
        except (ValueError, KeyError, TypeError):
            x, y = self.root.winfo_screenwidth() - width - self.px(24), self.px(80)
        x, y = self.clamp(x, y)
        self.root.geometry(f"+{x}+{y}")

    def clamp(self, x, y):
        """Top-left position that keeps the whole window inside the nearest monitor's work area."""
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if w <= 1:  # not mapped yet
            w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        l, t, r, b = work_area(x + w // 2, y + h // 2) or virtual_screen(self.root)
        return max(l, min(x, r - w)), max(t, min(y, b - h))

    def keep_on_screen(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        nx, ny = self.clamp(x, y)
        if (nx, ny) != (x, y):
            self.root.geometry(f"+{nx}+{ny}")

    def guard(self):
        self.keep_on_screen()
        self.root.after(GUARD_MS, self.guard)

    def drag_start(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        dx, dy = self.drag
        x, y = self.clamp(e.x_root - dx, e.y_root - dy)
        self.root.geometry(f"+{x}+{y}")

    def save_state(self):
        self.state.update(x=self.root.winfo_x(), y=self.root.winfo_y())
        if self.view.order:
            self.state["order"] = self.view.order
        else:
            self.state.pop("order", None)
        try:
            STATE.write_text(json.dumps(self.state), encoding="utf-8")
        except OSError:
            pass

    def quit(self):
        self.save_state()
        self.view.close()
        self.root.destroy()


def main():
    if not single_instance():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # crisp on HiDPI
    except (AttributeError, OSError):
        pass
    Widget().root.mainloop()


if __name__ == "__main__":
    main()
