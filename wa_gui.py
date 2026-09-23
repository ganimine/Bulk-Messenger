import os
import re
import time
import random
import threading
import queue
from datetime import datetime

import pandas as pd
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager


# ============================================================
#  CORE WORKER (runs in background thread)
# ============================================================
class WhatsAppSender:
    def __init__(self, log_fn, progress_fn, done_fn, stop_event):
        self.log = log_fn                 # callable(str)
        self.progress = progress_fn       # callable(current, total, name)
        self.done = done_fn               # callable(sent, failed, skipped)
        self.stop_event = stop_event      # threading.Event
        self.driver = None
        self.paused = threading.Event()
        self.paused.set()                 # set = running, clear = paused

    # ---------- helpers ----------
    @staticmethod
    def clean_number(num, default_cc="91"):
        if pd.isna(num):
            return None
        s = re.sub(r"\D", "", str(num))
        if not s:
            return None
        if len(s) == 10:
            s = default_cc + s
        if len(s) < 11 or len(s) > 15:
            return None
        return s

    def _build_driver(self, profile_dir):
        opts = Options()
        os.makedirs(profile_dir, exist_ok=True)
        opts.add_argument(f"--user-data-dir={os.path.abspath(profile_dir)}")
        opts.add_argument("--start-maximized")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return driver

    def _wait_ready(self, timeout=300):
        self.log("⏳ Waiting for WhatsApp Web to be ready (scan QR if needed)...")
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.ID, "pane-side"))
            )
            self.log("✅ WhatsApp Web is ready.")
            return True
        except TimeoutException:
            self.log("❌ Timed out waiting for WhatsApp Web.")
            return False

    def _open_chat(self, number, timeout=20):
        self.driver.get(f"https://web.whatsapp.com/send?phone={number}")
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//div[@contenteditable="true"][@data-tab="10"]')
                )
            )
            return True
        except TimeoutException:
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'div[contenteditable="true"]')
                    )
                )
                return True
            except TimeoutException:
                return False

    def _send_message(self, message):
        try:
            box = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//div[@contenteditable="true"][@data-tab="10"]')
                )
            )
            box.click()
            time.sleep(0.4)
            for i, line in enumerate(message.split("\n")):
                if i > 0:
                    box.send_keys(Keys.SHIFT + Keys.ENTER)
                box.send_keys(line)
            time.sleep(0.6)
            box.send_keys(Keys.ENTER)
            time.sleep(4)
            return True
        except Exception as e:
            self.log(f"   ⚠️ send error: {e}")
            return False

    def _respect_pause_stop(self):
        """Block while paused; return False if stop requested."""
        while not self.paused.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.3)
        return not self.stop_event.is_set()

    # ---------- main entry ----------
    def run(self, excel_path, profile_dir, delay_min, delay_max,
            skip_sent=True, default_cc="91"):
        sent = failed = skipped = 0
        try:
            if not os.path.exists(excel_path):
                self.log(f"❌ File not found: {excel_path}")
                self.done(0, 0, 0)
                return

            df = pd.read_excel(excel_path)
            df.columns = [c.strip() for c in df.columns]

            required = ["Name of the candidate", "Mobile No", "sms", "Whats App", "Status"]
            missing = [c for c in required if c not in df.columns]
            if missing:
                self.log(f"❌ Missing columns: {missing}")
                self.done(0, 0, 0)
                return

            if "Status" not in df.columns:
                df["Status"] = ""

            total = len(df)
            self.log(f"📄 Loaded {total} rows from {os.path.basename(excel_path)}")

            # Launch browser
            self.driver = self._build_driver(profile_dir)
            self.driver.get("https://web.whatsapp.com")
            if not self._wait_ready():
                self.done(0, 0, 0)
                return

            for i, row in df.iterrows():
                if self.stop_event.is_set():
                    self.log("🛑 Stop requested by user.")
                    break
                if not self._respect_pause_stop():
                    break

                name = str(row["Name of the candidate"]).strip()
                msg = str(row["sms"]).strip()
                status = str(row["Status"]).strip().lower() if not pd.isna(row["Status"]) else ""

                if skip_sent and status == "sent":
                    self.log(f"⏭️  [{i+1}/{total}] Skipping {name} (already sent)")
                    skipped += 1
                    self.progress(i + 1, total, name)
                    continue

                number = (self.clean_number(row["Whats App"], default_cc)
                          or self.clean_number(row["Mobile No"], default_cc))

                if not number:
                    self.log(f"⚠️  [{i+1}/{total}] Invalid number for {name}")
                    df.at[i, "Status"] = "invalid number"
                    failed += 1
                    df.to_excel(excel_path, index=False)
                    self.progress(i + 1, total, name)
                    continue

                self.log(f"📤 [{i+1}/{total}] Sending to {name} ({number}) ...")

                if not self._open_chat(number):
                    self.log(f"   ❌ Could not open chat for {name}")
                    df.at[i, "Status"] = "chat open failed"
                    failed += 1
                    df.to_excel(excel_path, index=False)
                    self.progress(i + 1, total, name)
                    continue

                time.sleep(1.5)

                if self._send_message(msg):
                    self.log(f"   ✅ Sent to {name}")
                    df.at[i, "Status"] = "sent"
                    sent += 1
                else:
                    self.log(f"   ❌ Failed for {name}")
                    df.at[i, "Status"] = "send failed"
                    failed += 1

                df.to_excel(excel_path, index=False)
                self.progress(i + 1, total, name)

                if self.stop_event.is_set():
                    break

                delay = random.uniform(delay_min, delay_max)
                self.log(f"   ⏱️  Waiting {delay:.1f}s ...")
                # pause-aware sleep
                end = time.time() + delay
                while time.time() < end:
                    if self.stop_event.is_set():
                        break
                    if not self.paused.is_set():
                        end = time.time() + delay  # reset on pause
                    time.sleep(0.3)

            self.log(f"\n🎉 Done. Sent: {sent} | Failed: {failed} | Skipped: {skipped}")

        except Exception as e:
            self.log(f"❌ Fatal error: {e}")
        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass
            self.done(sent, failed, skipped)


# ============================================================
#  GUI
# ============================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WhatsApp Bulk Sender — Selenium + Tkinter")
        self.geometry("900x680")
        self.minsize(820, 600)

        self.excel_path = tk.StringVar()
        self.profile_dir = tk.StringVar(value=os.path.abspath("wa_profile"))
        self.delay_min = tk.IntVar(value=12)
        self.delay_max = tk.IntVar(value=25)
        self.skip_sent = tk.BooleanVar(value=True)
        self.cc = tk.StringVar(value="91")

        self.stop_event = threading.Event()
        self.worker_thread = None
        self.sender = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self.after(100, self._drain_log_queue)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # ---- File selection ----
        frm_file = ttk.LabelFrame(self, text="1. Excel File")
        frm_file.pack(fill="x", **pad)

        ttk.Entry(frm_file, textvariable=self.excel_path).pack(
            side="left", fill="x", expand=True, padx=8, pady=8
        )
        ttk.Button(frm_file, text="Browse...", command=self.pick_file).pack(
            side="left", padx=8, pady=8
        )

        # ---- Settings ----
        frm_set = ttk.LabelFrame(self, text="2. Settings")
        frm_set.pack(fill="x", **pad)

        row1 = ttk.Frame(frm_set); row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text="Min delay (s):").pack(side="left")
        ttk.Spinbox(row1, from_=5, to=600, width=6,
                    textvariable=self.delay_min).pack(side="left", padx=(4, 20))
        ttk.Label(row1, text="Max delay (s):").pack(side="left")
        ttk.Spinbox(row1, from_=5, to=600, width=6,
                    textvariable=self.delay_max).pack(side="left", padx=(4, 20))
        ttk.Label(row1, text="Country code:").pack(side="left")
        ttk.Entry(row1, textvariable=self.cc, width=5).pack(side="left", padx=4)

        row2 = ttk.Frame(frm_set); row2.pack(fill="x", padx=8, pady=4)
        ttk.Checkbutton(row2, text="Skip rows already marked 'sent'",
                        variable=self.skip_sent).pack(side="left")
        ttk.Label(row2, text="Chrome profile:").pack(side="left", padx=(20, 4))
        ttk.Entry(row2, textvariable=self.profile_dir).pack(
            side="left", fill="x", expand=True
        )

        # ---- Buttons ----
        frm_btn = ttk.Frame(self)
        frm_btn.pack(fill="x", **pad)

        self.btn_start = ttk.Button(frm_btn, text="▶ Start Sending",
                                    command=self.start_sending)
        self.btn_start.pack(side="left", padx=4)

        self.btn_pause = ttk.Button(frm_btn, text="⏸ Pause",
                                    command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=4)

        self.btn_stop = ttk.Button(frm_btn, text="⏹ Stop",
                                   command=self.stop_sending, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        self.lbl_state = ttk.Label(frm_btn, text="Idle", foreground="gray")
        self.lbl_state.pack(side="right", padx=8)

        # ---- Progress ----
        frm_prog = ttk.LabelFrame(self, text="3. Progress")
        frm_prog.pack(fill="x", **pad)

        self.progress = ttk.Progressbar(frm_prog, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=(8, 4))

        self.lbl_progress = ttk.Label(frm_prog, text="0 / 0")
        self.lbl_progress.pack(anchor="w", padx=8, pady=(0, 8))

        # ---- Log ----
        frm_log = ttk.LabelFrame(self, text="4. Live Log")
        frm_log.pack(fill="both", expand=True, **pad)

        self.txt_log = scrolledtext.ScrolledText(
            frm_log, wrap="word", height=18, state="disabled",
            font=("Consolas", 9)
        )
        self.txt_log.pack(fill="both", expand=True, padx=8, pady=8)
        self.txt_log.tag_config("ok", foreground="green")
        self.txt_log.tag_config("err", foreground="red")
        self.txt_log.tag_config("warn", foreground="#b36b00")

    # ---------- Actions ----------
    def pick_file(self):
        path = filedialog.askopenfilename(
            title="Select Excel file",
            filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")]
        )
        if path:
            self.excel_path.set(path)

    def start_sending(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Already running", "Sender is already running.")
            return
        if not self.excel_path.get() or not os.path.exists(self.excel_path.get()):
            messagebox.showerror("No file", "Please choose a valid Excel file.")
            return
        if self.delay_min.get() > self.delay_max.get():
            messagebox.showerror("Bad delays", "Min delay must be <= Max delay.")
            return

        self._clear_log()
        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal", text="⏸ Pause")
        self.btn_stop.config(state="normal")
        self.lbl_state.config(text="Starting...", foreground="blue")

        self.sender = WhatsAppSender(
            log_fn=self._log_from_thread,
            progress_fn=self._progress_from_thread,
            done_fn=self._done_from_thread,
            stop_event=self.stop_event,
        )

        self.worker_thread = threading.Thread(
            target=self.sender.run,
            args=(
                self.excel_path.get(),
                self.profile_dir.get(),
                self.delay_min.get(),
                self.delay_max.get(),
                self.skip_sent.get(),
                self.cc.get().strip() or "91",
            ),
            daemon=True,
        )
        self.worker_thread.start()

    def toggle_pause(self):
        if not self.sender:
            return
        if self.sender.paused.is_set():
            self.sender.paused.clear()
            self.btn_pause.config(text="▶ Resume")
            self.lbl_state.config(text="Paused", foreground="#b36b00")
            self._log("⏸ Paused by user.")
        else:
            self.sender.paused.set()
            self.btn_pause.config(text="⏸ Pause")
            self.lbl_state.config(text="Running...", foreground="blue")
            self._log("▶ Resumed.")

    def stop_sending(self):
        if self.sender:
            self.sender.paused.set()   # unblock pause loop
        self.stop_event.set()
        self.lbl_state.config(text="Stopping...", foreground="red")
        self._log("🛑 Stop requested. Finishing current message...")

    # ---------- Thread-safe updates ----------
    def _log_from_thread(self, msg):
        self.log_queue.put(("log", msg))

    def _progress_from_thread(self, cur, total, name):
        self.log_queue.put(("progress", (cur, total, name)))

    def _done_from_thread(self, sent, failed, skipped):
        self.log_queue.put(("done", (sent, failed, skipped)))

    def _drain_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    cur, total, name = payload
                    self.progress["maximum"] = total
                    self.progress["value"] = cur
                    self.lbl_progress.config(
                        text=f"{cur} / {total}   ({name})"
                    )
                elif kind == "done":
                    sent, failed, skipped = payload
                    self._on_done(sent, failed, skipped)
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.txt_log.config(state="normal")
        tag = None
        if "✅" in msg or "🎉" in msg:
            tag = "ok"
        elif "❌" in msg or "⚠️" in msg:
            tag = "err"
        elif "⏭️" in msg or "🛑" in msg or "⏸" in msg:
            tag = "warn"
        self.txt_log.insert("end", line, tag if tag else "")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _clear_log(self):
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")
        self.progress["value"] = 0
        self.lbl_progress.config(text="0 / 0")

    def _on_done(self, sent, failed, skipped):
        self.btn_start.config(state="normal")
        self.btn_pause.config(state="disabled", text="⏸ Pause")
        self.btn_stop.config(state="disabled")
        self.lbl_state.config(text="Finished", foreground="green")
        messagebox.showinfo(
            "Completed",
            f"Sent: {sent}\nFailed: {failed}\nSkipped: {skipped}\n\n"
            "The Excel file's Status column has been updated."
        )


# ============================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()