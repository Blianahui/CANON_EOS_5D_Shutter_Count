"""Window for eos5d_shutter_count_auto2.py.

The button runs the script's own main() unchanged; everything it prints goes to
the log window and to a log file beside the program.
"""

from __future__ import annotations

import datetime
import os
import queue
import re
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import eos5d_shutter_count_auto2 as reader

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
LOG_PATH = os.path.join(APP_DIR, "shutter_count_log.txt")

messages: "queue.Queue[tuple[str, str]]" = queue.Queue()


class LogWriter:
    """Stands in for stdout/stderr: hands text to the window and the log file."""

    def __init__(self):
        self._lock = threading.Lock()
        self._line_start = True

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            stamped = []
            for part in text.splitlines(keepends=True):
                if self._line_start:
                    stamped.append(datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] "))
                stamped.append(part)
                self._line_start = part.endswith("\n")
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write("".join(stamped))
            except OSError:
                pass
        messages.put(("log", text))
        return len(text)

    def flush(self):
        pass


def camera_state():
    """Read-only look at Windows' device list, using the script's own helpers."""
    try:
        if reader.instance_id(reader.DCP_PID):
            return "service", reader.service(reader.DCP_PID) or "no driver"
        if reader.instance_id(reader.EOS5D_PID):
            return "normal", reader.service(reader.EOS5D_PID) or "no driver"
        return "none", ""
    except Exception as exc:
        return "error", str(exc)


def run_check():
    """Same as the script's _run(), with output captured."""
    print("=" * 60, flush=True)
    try:
        reader.main()
        ok = True
    except Exception as exc:
        print(f"\nFATAL: {exc}", flush=True)
        if reader.VERBOSE:
            traceback.print_exc()
        ok = False
    messages.put(("done", "ok" if ok else "fail"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("EOS 5D Shutter Count")
        self.geometry("720x560")
        self.minsize(560, 420)
        self.busy = False
        self.output = ""

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        self.button = ttk.Button(top, text="Check shutter count", command=self.start)
        self.button.pack(side="left", ipadx=10, ipady=6)

        status_box = ttk.LabelFrame(top, text="Camera", padding=(10, 4))
        status_box.pack(side="left", fill="x", expand=True, padx=(12, 0))
        status_row = ttk.Frame(status_box)
        status_row.pack(fill="x")
        self.dot = tk.Canvas(status_row, width=14, height=14, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill="#999", outline="")
        self.dot.pack(side="left")
        self.status = ttk.Label(status_row, text="Checking...", font=("Segoe UI", 10, "bold"))
        self.status.pack(side="left", padx=6)
        self.driver = ttk.Label(status_box, text="", foreground="#555")
        self.driver.pack(anchor="w")

        count_box = ttk.LabelFrame(self, text="Shutter count", padding=10)
        count_box.pack(fill="x", padx=12)
        self.count = ttk.Label(count_box, text="—", font=("Segoe UI", 28, "bold"), anchor="center")
        self.count.pack(fill="x")
        self.note = ttk.Label(count_box, text="", foreground="#555", anchor="center")
        self.note.pack(fill="x")

        log_box = ttk.LabelFrame(self, text="Log", padding=6)
        log_box.pack(fill="both", expand=True, padx=12, pady=12)
        self.log = ScrolledText(log_box, height=12, font=("Consolas", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("error", foreground="#b00020")
        ttk.Label(log_box, text=f"Log file: {LOG_PATH}", foreground="#555").pack(anchor="w", pady=(4, 0))

        self.after(100, self.pump)
        self.poll_status()

    def start(self):
        if self.busy:
            return
        self.busy = True
        self.output = ""
        self.button.state(["disabled"])
        self.count.configure(text="...")
        self.note.configure(text="Working, please wait")
        threading.Thread(target=run_check, daemon=True).start()

    def append(self, text):
        self.output += text
        self.log.configure(state="normal")
        for line in text.splitlines(keepends=True):
            tag = "error" if re.match(r"\s*(ERROR|FATAL|WARNING)", line) else None
            self.log.insert("end", line, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def finish(self, result):
        self.busy = False
        self.button.state(["!disabled"])
        m = re.search(r"SHUTTER COUNT: (\d+)", self.output)
        if result == "ok" and m:
            self.count.configure(text=f"{int(m.group(1)):,}".replace(",", " "))
            self.note.configure(text="Accurate to within 255 (live byte is 0xFF)"
                                if "live byte is 0xFF" in self.output else "")
        else:
            self.count.configure(text="Failed")
            self.note.configure(text="See the log below")
        self.poll_status(once=True)

    def pump(self):
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "log":
                    self.append(value)
                elif kind == "status":
                    self.show_status(*value)
                else:
                    self.finish(value)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def show_status(self, mode, driver):
        text, colour = {
            "normal": ("Connected - normal mode", "#2e7d32"),
            "service": ("Connected - service mode", "#ef6c00"),
            "none": ("Not connected", "#9e9e9e"),
            "error": ("Cannot read device list", "#b00020"),
        }[mode]
        self.status.configure(text=text)
        self.dot.itemconfigure(self.dot_id, fill=colour)
        self.driver.configure(text=f"Driver: {driver}" if mode in ("normal", "service") else
                              (driver if mode == "error" else ""))

    def poll_status(self, once=False):
        threading.Thread(target=lambda: messages.put(("status", camera_state())), daemon=True).start()
        if not once:
            self.after(1500, self.poll_status)


def main():
    writer = LogWriter()
    sys.stdout = writer
    sys.stderr = writer
    App().mainloop()


if __name__ == "__main__":
    main()
