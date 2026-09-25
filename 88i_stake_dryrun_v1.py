#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

TARGET = "https://www.88idd.com/home/#/lottery?tabName=Lottery&id=77"
BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "88i_browser_profile"


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def is_logged_in(page):
    txt = body_text(page)
    return (
        "Nạp tiền" in txt
        and "Rút tiền" in txt
        and not ("Tài khoản" in txt and "Đăng nhập" in txt)
    )


def get_visible_numbers(page):
    nums = []
    for n in range(10):
        loc = page.locator(f"li.number.bg-number_{n}").first
        try:
            if loc.count() and loc.is_visible():
                nums.append(n)
        except Exception:
            pass
    return nums


def wait_numbers(page, timeout_ms=7000):
    elapsed = 0
    while elapsed < timeout_ms:
        nums = get_visible_numbers(page)
        if len(nums) == 10:
            return True, nums
        page.wait_for_timeout(500)
        elapsed += 500
    return False, get_visible_numbers(page)


def ensure_numbers(page, max_reloads=3):
    for attempt in range(max_reloads + 1):
        ok, nums = wait_numbers(page)
        if ok:
            return True, nums, attempt

        if attempt >= max_reloads:
            break

        print(f"NUMBER PANEL chưa đủ ({nums}). Reload {attempt+1}/{max_reloads}...")
        page.reload(wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        if not is_logged_in(page):
            return False, [], attempt + 1

    return False, get_visible_numbers(page), max_reloads


def parse_numbers(raw):
    nums = [int(x.strip()) for x in raw.split(",") if x.strip()]
    if len(nums) != 7:
        raise SystemExit("Cần đúng 7 số.")
    if len(set(nums)) != 7:
        raise SystemExit("7 số phải khác nhau.")
    if any(n < 0 or n > 9 for n in nums):
        raise SystemExit("Mỗi số phải nằm trong 0..9.")
    return nums


def parse_money(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--numbers", required=True, help="Ví dụ: 0,1,2,3,4,5,6")
    ap.add_argument("--stake", type=int, default=3000, help="Tiền mỗi số, mặc định 3000")
    ap.add_argument("--screenshot", default="88i_stake_dryrun_v1.png")
    args = ap.parse_args()

    nums = parse_numbers(args.numbers)
    if args.stake <= 0:
        raise SystemExit("--stake phải > 0")

    expected_total = args.stake * 7

    if not PROFILE_DIR.exists():
        raise SystemExit("Thiếu 88i_browser_profile.")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1600, "height": 1000},
        )

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        if not is_logged_in(page):
            print("LOGIN: LOGGED_OUT")
            input("Nhấn ENTER để đóng...")
            ctx.close()
            return

        print("LOGIN: OK")

        ok, visible, reloads = ensure_numbers(page, max_reloads=3)
        if not ok:
            print("NUMBER PANEL: FAIL", visible)
            input("Nhấn ENTER để đóng...")
            ctx.close()
            return

        print("NUMBER PANEL: OK")
        print("AUTO RELOADS:", reloads)

        # Chọn đúng 7 số.
        for n in nums:
            loc = page.locator(f"li.number.bg-number_{n}").first
            if not loc.count() or not loc.is_visible():
                print(f"STOP: số {n} không visible.")
                input("Nhấn ENTER để đóng...")
                ctx.close()
                return
            loc.click()
            page.wait_for_timeout(160)

        page.wait_for_timeout(700)

        # Kiểm tra 7 input cược riêng.
        row_inputs = page.locator('div.myOrder input[type="tel"]')
        row_count = row_inputs.count()
        print("ROW INPUTS:", row_count)

        if row_count != 7:
            print("STOP: không tìm thấy đúng 7 input tiền của 7 số.")
            input("Nhấn ENTER để đóng...")
            ctx.close()
            return

        # Input "Cược đơn" ở cuối panel. Đây là control toàn cục.
        global_input = page.locator('div.single-bet-wp input[type="text"]').first

        if global_input.count() == 0:
            print("STOP: không tìm thấy input Cược đơn.")
            input("Nhấn ENTER để đóng...")
            ctx.close()
            return

        old_global = global_input.input_value()
        print("GLOBAL STAKE BEFORE:", old_global)

        # Fill bằng số thô; website tự format dấu phẩy.
        global_input.click()
        global_input.fill(str(args.stake))
        global_input.press("Tab")
        page.wait_for_timeout(900)

        new_global = global_input.input_value()
        row_values = [row_inputs.nth(i).input_value() for i in range(row_count)]
        row_amounts = [parse_money(v) for v in row_values]

        bet_btn = page.locator("button.el-button.bet-btn.el-button--primary").first
        bet_text = bet_btn.inner_text().strip() if bet_btn.count() else ""
        bet_total = parse_money(bet_text)

        print("GLOBAL STAKE AFTER:", new_global)
        print("ROW VALUES:", row_values)
        print("EXPECTED TOTAL:", f"{expected_total:,}")
        print("BET BUTTON:", bet_text or "(không đọc được)")

        all_rows_ok = all(v == args.stake for v in row_amounts)
        total_ok = bet_total == expected_total

        print("VERIFY ROWS:", "OK" if all_rows_ok else "FAIL")
        print("VERIFY TOTAL:", "OK" if total_ok else "FAIL")

        page.screenshot(path=args.screenshot, full_page=True)
        print("SCREENSHOT:", args.screenshot)

        if all_rows_ok and total_ok:
            print("DRY RUN PASS: tiền mỗi số và tổng tiền đều đúng.")
        else:
            print("DRY RUN FAIL: KHÔNG được bật real bet.")

        print()
        print("KHÔNG click nút Đặt Cược. KHÔNG gửi lệnh cược.")
        input("Kiểm tra browser/screenshot rồi nhấn ENTER để đóng...")
        ctx.close()


if __name__ == "__main__":
    main()
