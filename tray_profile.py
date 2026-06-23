"""App khay hệ thống (system tray) cho crawler Hồ Sơ Năng Lực muasamcong.

Chạy: python tray_profile.py

- Thu nhỏ xuống khay khi bấm X.
- Tự động quét định kỳ mỗi 2 giờ.
- Chuột phải icon khay → Chạy ngay / Mở giao diện / Thoát.
- Chỉ cho phép 1 instance (khóa cổng 50574).
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import winreg
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, scrolledtext, messagebox

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False

BASE   = Path(__file__).resolve().parent
PYEXE  = sys.executable
RUN    = str(BASE / "run.py")

_LOCK_PORT  = 50574
_SCHED_INTERVAL_HOURS = 2    # quét định kỳ mỗi 2 giờ
_APP_NAME   = "CrawlProfile"
_APP_TITLE  = "Crawler Hồ Sơ Năng Lực — muasamcong"


# ── Icon khay ────────────────────────────────────────────────────────────────

def _tray_image() -> "Image.Image":
    img = Image.new("RGBA", (64, 64), (25, 95, 170, 255))   # xanh dương
    d = ImageDraw.Draw(img)
    d.rectangle([4, 4, 60, 60], outline=(255, 255, 255, 180), width=2)
    try:
        from PIL import ImageFont
        font = None
        for name in ("arialbd.ttf", "arial.ttf", "segoeui.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(name, 38)
                break
            except Exception:
                pass
        if font:
            bbox = d.textbbox((0, 0), "P", font=font)
            x = (64 - (bbox[2] - bbox[0])) // 2 - bbox[0]
            y = (64 - (bbox[3] - bbox[1])) // 2 - bbox[1]
            d.text((x, y), "P", fill=(255, 255, 255, 255), font=font)
        else:
            d.text((22, 20), "P", fill=(255, 255, 255, 255))
    except Exception:
        pass
    return img


# ── Single instance ───────────────────────────────────────────────────────────

def _acquire_lock():
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", _LOCK_PORT))
        srv.listen(5)
        return srv
    except OSError:
        srv.close()
        try:
            c = socket.create_connection(("127.0.0.1", _LOCK_PORT), timeout=1)
            c.sendall(b"show"); c.close()
        except Exception:
            pass
        return None


def _lock_listener(srv, app):
    while True:
        try:
            conn, _ = srv.accept()
            try: conn.recv(16)
            except Exception: pass
            conn.close()
            app.root.after(0, app._show_window)
        except Exception:
            break


# ── Startup registry ──────────────────────────────────────────────────────────

def _startup_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run")
        winreg.QueryValueEx(key, _APP_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def _set_startup(enable: bool):
    reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path,
                             0, winreg.KEY_SET_VALUE)
        if enable:
            cmd = f'"{PYEXE}" "{__file__}"'
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        messagebox.showerror("Lỗi Registry", str(e))


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _sched_interval() -> timedelta:
    """Khoảng cách giữa 2 lần quét định kỳ."""
    return timedelta(hours=_SCHED_INTERVAL_HOURS)


def _load_delay() -> float:
    """Đọc delay/request đã lưu (giây) để hiển thị lên GUI."""
    try:
        from crawler import config as C
        return C.get_request_delay()
    except Exception:
        return 1.0


# ── App chính ─────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self._next_run_at: datetime = datetime.now() + _sched_interval()
        self._tray_notified = False
        self.tray = None

        root.title(_APP_TITLE)
        root.geometry("860x620")
        root.minsize(720, 500)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._setup_tray()

        # bắt đầu scheduler và poll
        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        root.after(150, self._poll)
        root.after(1000, self._tick_next_run)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        pad = {"padx": 6, "pady": 4}

        # ── Thanh trạng thái + nút ───────────────────────────────────────────
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill="x")

        self.lbl_next = ttk.Label(top, text="Lần chạy tiếp: ...",
                                  font=("Segoe UI", 10))
        self.lbl_next.pack(side="left")

        self.btn_run = ttk.Button(top, text="▶  Chạy ngay", command=self.run_now)
        self.btn_run.pack(side="right")
        self.btn_stop = ttk.Button(top, text="■  Dừng", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="right", padx=6)

        self.v_startup = tk.BooleanVar(value=_startup_enabled())
        ttk.Checkbutton(top, text="Khởi động cùng Windows",
                        variable=self.v_startup,
                        command=self._toggle_startup).pack(side="right", padx=10)

        # ── Bảng trạng thái từng nguồn ───────────────────────────────────────
        tbl_frame = ttk.LabelFrame(self.root, text="Trạng thái từng nguồn", padding=6)
        tbl_frame.pack(fill="x", padx=10, pady=(4, 6))

        cols = ("Nguồn", "Tên", "Lần cuối chạy", "Tổng bản ghi", "Mới/Cập nhật")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings", height=6)
        for col in cols:
            self.tree.heading(col, text=col)
        self.tree.column("Nguồn",          width=130, anchor="w")
        self.tree.column("Tên",            width=210, anchor="w")
        self.tree.column("Lần cuối chạy", width=130, anchor="center")
        self.tree.column("Tổng bản ghi",  width=110, anchor="center")
        self.tree.column("Mới/Cập nhật",  width=110, anchor="center")
        self.tree.pack(fill="x")

        try:
            from crawler import config as C
            for key, info in C.SOURCES.items():
                self.tree.insert("", "end", iid=key,
                                 values=(key, info["name"], "—", "—", "—"))
        except Exception:
            pass

        # ── Cấu hình crawl ───────────────────────────────────────────────────
        cfg_frame = ttk.LabelFrame(self.root, text="Cấu hình", padding=6)
        cfg_frame.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(cfg_frame,
                  text="Delay mỗi request tới muasamcong (giây):").pack(side="left")
        self.v_delay = tk.StringVar(value=str(_load_delay()))
        ttk.Spinbox(cfg_frame, from_=0.0, to=10.0, increment=0.5, width=6,
                    textvariable=self.v_delay).pack(side="left", padx=(6, 12))
        ttk.Button(cfg_frame, text="💾  Lưu cấu hình",
                   command=self._save_config).pack(side="left")
        self.lbl_cfg = ttk.Label(cfg_frame, text="", foreground="gray")
        self.lbl_cfg.pack(side="left", padx=10)

        # ── Thanh trạng thái dưới ────────────────────────────────────────────
        status_bar = ttk.Frame(self.root, padding=(10, 0))
        status_bar.pack(fill="x")
        self.lbl_status = ttk.Label(status_bar, text="● Sẵn sàng",
                                    foreground="gray")
        self.lbl_status.pack(side="left")

        # ── Log ──────────────────────────────────────────────────────────────
        ttk.Button(status_bar, text="🧹 Xóa log",
                   command=self.clear_log).pack(side="right")
        self.log = scrolledtext.ScrolledText(
            self.root, height=18, font=("Consolas", 9),
            state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=10, pady=(2, 8))

        self._refresh_source_table()

    # ── Tray ─────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        if not _HAS_TRAY:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Mở giao diện", self._tray_show, default=True),
                pystray.MenuItem("Chạy ngay",    self._tray_run),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Thoát",         self._tray_quit),
            )
            self.tray = pystray.Icon(_APP_NAME, _tray_image(), _APP_TITLE, menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def _tray_show(self, *_):
        self.root.after(0, self._show_window)

    def _tray_run(self, *_):
        self.root.after(0, self.run_now)

    def _tray_quit(self, *_):
        self.root.after(0, self._quit_app)

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    # ── Khởi động cùng Windows ────────────────────────────────────────────────

    def _toggle_startup(self):
        _set_startup(self.v_startup.get())

    # ── Lưu cấu hình ──────────────────────────────────────────────────────────

    def _save_config(self):
        from crawler import config as C
        try:
            d = float(self.v_delay.get())
            if d < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Cấu hình không hợp lệ",
                                 "Delay phải là số ≥ 0 (giây). Ví dụ: 1.0")
            return
        cfg = C.load_app_config()
        cfg["request_delay"] = d
        try:
            C.save_app_config(cfg)
            self.v_delay.set(str(d))
            self.lbl_cfg.config(text=f"✓ Đã lưu (delay = {d}s)", foreground="green")
            self._append(f"[Cấu hình] Đã lưu delay = {d}s "
                         f"(áp dụng cho lần crawl tiếp theo)\n")
        except Exception as e:
            messagebox.showerror("Lỗi lưu cấu hình", str(e))

    # ── Chạy / dừng ──────────────────────────────────────────────────────────

    def run_now(self):
        if self.proc:
            messagebox.showinfo("Đang chạy", "Crawler đang chạy, vui lòng chờ.")
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # Truyền delay hiện trên GUI để có hiệu lực ngay (kể cả chưa bấm Lưu)
        try:
            env["PROFILE_REQUEST_DELAY"] = str(float(self.v_delay.get()))
        except (ValueError, AttributeError):
            pass
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        cmd = [PYEXE, "-u", RUN]
        self._append(f"\n>>> {' '.join(cmd)}\n\n")
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env, cwd=str(BASE), creationflags=flags,
            )
        except Exception as e:
            messagebox.showerror("Lỗi khởi chạy", str(e)); return
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.lbl_status.config(text="● Đang chạy…", foreground="green")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line)
        except Exception as e:
            self.q.put(f"[đọc log lỗi] {e}\n")
        self.q.put(("__DONE__", self.proc.poll() if self.proc else None))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item[0] == "__DONE__":
                    self._on_done(item[1])
                else:
                    self._append(item)
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    def _on_done(self, rc):
        self._append(f"\n=== KẾT THÚC (rc={rc}) ===\n")
        self.proc = None
        self.btn_run.config(state="normal")
        self.btn_stop.config(state="disabled")
        color = "gray" if rc == 0 else "red"
        self.lbl_status.config(text=f"● Xong (rc={rc})", foreground=color)
        self._refresh_source_table()
        if self.tray:
            try:
                self.tray.notify(
                    f"Crawl hồ sơ xong (rc={rc}). Mở giao diện để xem chi tiết.",
                    _APP_TITLE)
            except Exception:
                pass

    def stop(self):
        if not self.proc:
            return
        self.lbl_status.config(text="● Đang dừng…", foreground="orange")
        self._append("\n[Đang dừng…]\n")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.proc.terminate()
        except Exception:
            pass

    # ── Scheduler (định kỳ mỗi N giờ) ─────────────────────────────────────────

    def _scheduler_loop(self):
        import time
        while True:
            time.sleep(30)
            if datetime.now() >= self._next_run_at:
                # Đặt lịch lần kế tiếp ngay để tránh chạy chồng / dồn nhiều lần
                self._next_run_at = datetime.now() + _sched_interval()
                self.root.after(0, self._auto_run)

    def _auto_run(self):
        if self.proc:
            self._append("\n[Bỏ qua lần quét định kỳ — crawler đang chạy]\n")
            return
        self._append(f"\n[Tự động quét định kỳ — mỗi {_SCHED_INTERVAL_HOURS}h]\n")
        self.run_now()

    def _tick_next_run(self):
        """Cập nhật nhãn đếm ngược đến lần chạy tiếp theo (mỗi giây)."""
        secs = max(0, int((self._next_run_at - datetime.now()).total_seconds()))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        self.lbl_next.config(
            text=f"Lần chạy tiếp: {self._next_run_at.strftime('%d/%m %H:%M')}  "
                 f"(còn {h:02d}:{m:02d}:{s:02d})")
        self.root.after(1000, self._tick_next_run)

    # ── Bảng trạng thái nguồn ────────────────────────────────────────────────

    def _refresh_source_table(self):
        try:
            from crawler.db import ProfileDB
            from crawler import config as C
            with ProfileDB() as db:
                for key in C.SOURCES:
                    log = db.last_log(key)
                    total = db.count(key)
                    if log:
                        ts = log.get("finished_at") or log.get("started_at")
                        ts_str = C.fmt_vn(ts)
                        delta = f"+{log.get('n_new',0)} / ~{log.get('n_updated',0)}"
                    else:
                        ts_str, delta = "—", "—"
                    self.tree.item(key, values=(
                        key,
                        C.SOURCES[key]["name"],
                        ts_str,
                        f"{total:,}" if total else "—",
                        delta,
                    ))
        except Exception:
            pass

    # ── Tiện ích ─────────────────────────────────────────────────────────────

    def _append(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ── Đóng / thoát ─────────────────────────────────────────────────────────

    def _on_close(self):
        if self.tray:
            self.root.withdraw()
            if not self._tray_notified:
                self._tray_notified = True
                try:
                    self.tray.notify(
                        "Phần mềm vẫn chạy ở khay hệ thống.\n"
                        "Chuột phải icon → Thoát để tắt hẳn.", _APP_TITLE)
                except Exception:
                    pass
            return
        self._quit_app()

    def _quit_app(self):
        if self.proc:
            self._show_window()
            if not messagebox.askyesno("Thoát hẳn",
                                       "Đang crawl. Dừng lại và thoát hẳn?"):
                return
            self.stop()
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.root.destroy()


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    lock = _acquire_lock()
    if lock is None:
        print(f"{_APP_TITLE} đang chạy rồi — đã đưa cửa sổ lên.")
        return

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass

    app = App(root)
    app._lock = lock
    threading.Thread(target=_lock_listener, args=(lock, app), daemon=True).start()

    # Ẩn cửa sổ ngay — chỉ hiện icon tray, mở cửa sổ khi cần
    root.withdraw()

    root.mainloop()


if __name__ == "__main__":
    main()
