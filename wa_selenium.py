import os
import re
import time
import random
import pandas as pd

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager

# ================= CONFIG =================
EXCEL_FILE       = "candidates.xlsx"
SHEET_NAME       = 0
DEFAULT_CC       = "91"          # country code for 10-digit numbers
CHROME_PROFILE   = "wa_profile"  # local folder to persist login
QR_WAIT_SECONDS  = 180           # time to scan QR code
SEND_WAIT        = 6             # wait after clicking send
DELAY_MIN        = 12            # min seconds between messages
DELAY_MAX        = 25            # max seconds between messages
# ==========================================


def clean_number(num) -> str | None:
    """Return digits-only number with country code, or None if invalid."""
    if pd.isna(num):
        return None
    s = re.sub(r"\D", "", str(num))
    if not s:
        return None
    if len(s) == 10:
        s = DEFAULT_CC + s
    if len(s) < 11 or len(s) > 15:
        return None
    return s


def build_driver():
    """Launch Chrome with a persistent profile so you scan QR only once."""
    opts = Options()
    profile_dir = os.path.abspath(CHROME_PROFILE)
    os.makedirs(profile_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    # Keep it looking like a normal browser to reduce detection
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def wait_for_whatsapp_ready(driver, timeout=QR_WAIT_SECONDS):
    """Wait until WhatsApp Web is logged in (chat list visible)."""
    print("⏳ Waiting for WhatsApp Web to be ready...")
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.ID, "pane-side"))
        )
        print("✅ WhatsApp Web is ready.")
        return True
    except TimeoutException:
        print("❌ Timed out waiting for WhatsApp Web. Did you scan the QR?")
        return False


def open_chat(driver, number: str, timeout=20):
    """Open a chat with the given number using the wa.me-style URL."""
    url = f"https://web.whatsapp.com/send?phone={number}"
    driver.get(url)

    # Wait for the message input box to appear
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, '//div[@contenteditable="true"][@data-tab="10"]')
            )
        )
        return True
    except TimeoutException:
        # Fallback selector (WhatsApp changes DOM occasionally)
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'div[contenteditable="true"]')
                )
            )
            return True
        except TimeoutException:
            return False


def send_message(driver, message: str) -> bool:
    """Type the message and press Enter."""
    try:
        box = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located(
                (By.XPATH, '//div[@contenteditable="true"][@data-tab="10"]')
            )
        )
        box.click()
        time.sleep(0.5)

        # Preserve newlines by splitting
        for i, line in enumerate(message.split("\n")):
            if i > 0:
                box.send_keys(Keys.SHIFT + Keys.ENTER)
            box.send_keys(line)

        time.sleep(0.8)
        box.send_keys(Keys.ENTER)
        time.sleep(SEND_WAIT)
        return True
    except Exception as e:
        print(f"   ⚠️ send_message error: {e}")
        return False


def process_excel(driver):
    if not os.path.exists(EXCEL_FILE):
        print(f"❌ File not found: {EXCEL_FILE}")
        return

    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME)
    df.columns = [c.strip() for c in df.columns]

    required = ["Name of the candidate", "Mobile No", "sms", "Whats App", "Status"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"❌ Missing columns: {missing}")
        return

    print(f"📄 Loaded {len(df)} rows\n")

    sent, failed, skipped = 0, 0, 0

    for i, row in df.iterrows():
        name = str(row["Name of the candidate"]).strip()
        msg  = str(row["sms"]).strip()
        status = str(row["Status"]).strip().lower() if not pd.isna(row["Status"]) else ""

        if status == "sent":
            print(f"⏭️  [{i+1}] Skipping {name} (already sent)")
            skipped += 1
            continue

        number = clean_number(row["Whats App"]) or clean_number(row["Mobile No"])
        if not number:
            print(f"⚠️  [{i+1}] Invalid number for {name}")
            df.at[i, "Status"] = "invalid number"
            failed += 1
            continue

        print(f"📤 [{i+1}] Sending to {name} ({number}) ...")

        if not open_chat(driver, number):
            print(f"   ❌ Could not open chat for {name}")
            df.at[i, "Status"] = "chat open failed"
            failed += 1
            df.to_excel(EXCEL_FILE, index=False)
            continue

        time.sleep(2)  # small settle

        if send_message(driver, msg):
            print(f"   ✅ Sent")
            df.at[i, "Status"] = "sent"
            sent += 1
        else:
            print(f"   ❌ Send failed")
            df.at[i, "Status"] = "send failed"
            failed += 1

        # Save progress after each row
        df.to_excel(EXCEL_FILE, index=False)

        # Human-like random delay
        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        print(f"   ⏱️  Waiting {delay:.1f}s ...\n")
        time.sleep(delay)

    df.to_excel(EXCEL_FILE, index=False)
    print(f"\n🎉 Done. Sent: {sent} | Failed: {failed} | Skipped: {skipped}")
    print(f"📁 Updated: {EXCEL_FILE}")


def main():
    driver = build_driver()
    try:
        driver.get("https://web.whatsapp.com")
        if not wait_for_whatsapp_ready(driver):
            return
        input("\n▶️  Press ENTER to start sending messages...")
        process_excel(driver)
    finally:
        print("\n🔒 Closing browser in 5s ...")
        time.sleep(5)
        driver.quit()


if __name__ == "__main__":
    main()