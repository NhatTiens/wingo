#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
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


def launch_ctx(p, headless=False):
    PROFILE_DIR.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1600, "height": 1000},
    )


def get_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def get_visible_numbers(page):
    visible = []
    for n in range(10):
        loc = page.locator(f"li.number.bg-number_{n}").first
        try:
            if loc.count() and loc.is_visible():
                visible.append(n)
        except Exception:
            pass
    return visible


def wait_numbers(page, timeout_ms=7000):
    elapsed = 0
    step = 500

    while elapsed < timeout_ms:
        try:
            ballbox = page.locator("ul.ballBox").first
            if ballbox.count() and ballbox.is_visible():
                nums = get_visible_numbers(page)
                if len(nums) == 10:
                    try:
                        ballbox.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    return True, nums
        except Exception:
            pass

        page.wait_for_timeout(step)
        elapsed += step

    return False, get_visible_numbers(page)


def ensure_numbers_loaded(page, max_reloads=3):
    # 88i đôi khi vào trang lần đầu nhưng chưa render bảng 0-9.
    # Nếu thiếu bảng số, tự reload rồi chờ lại.
    for attempt in range(max_reloads + 1):
        ok, nums = wait_numbers(page)

        if ok:
            return True, nums, attempt

        if attempt >= max_reloads:
            break

        print(
            f"NUMBER PANEL chưa đủ ({nums}). "
            f"Tự reload trang lần {attempt + 1}/{max_reloads}..."
        )

        page.reload(wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        if not is_logged_in(page):
            return False, [], attempt + 1

    return False, get_visible_numbers(page), max_reloads


def init_profile():
    with sync_playwright() as p:
        ctx = launch_ctx(p, headless=False)
        page = get_page(ctx)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)

        print()
        print("Đăng nhập thủ công trên browser.")
        print("Sau khi thấy tên tài khoản + số dư, vào Win go 30 giây.")
        input("Sau đó quay lại PowerShell và nhấn ENTER: ")

        page.wait_for_timeout(1000)
        print("LOGIN:", "OK" if is_logged_in(page) else "CHƯA XÁC NHẬN")
        print("PROFILE:", PROFILE_DIR)

        ctx.close()


def check_profile(headless=False):
    if not PROFILE_DIR.exists():
        raise SystemExit(
            "Chưa có profile. Chạy: python 88i_profile_view_v2.py init"
        )

    with sync_playwright() as p:
        ctx = launch_ctx(p, headless=headless)
        page = get_page(ctx)
        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        ok = is_logged_in(page)
        print("LOGIN:", "OK" if ok else "LOGGED_OUT")
        print("URL:", page.url)

        if not headless:
            input("Nhấn ENTER để đóng browser...")

        ctx.close()
        raise SystemExit(0 if ok else 1)


def show_numbers():
    if not PROFILE_DIR.exists():
        raise SystemExit(
            "Chưa có profile. Chạy: python 88i_profile_view_v2.py init"
        )

    with sync_playwright() as p:
        ctx = launch_ctx(p, headless=False)
        page = get_page(ctx)

        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        if not is_logged_in(page):
            print("LOGIN: LOGGED_OUT")
            print("Hãy chạy lại init để đăng nhập profile.")
            input("Nhấn ENTER để đóng...")
            ctx.close()
            return

        print("LOGIN: OK")

        ok, nums, reloads = ensure_numbers_loaded(page, max_reloads=3)

        if ok:
            print("NUMBER PANEL: OK")
            print("VISIBLE:", nums)
            print("AUTO RELOADS:", reloads)
        else:
            print("NUMBER PANEL: FAIL")
            print("VISIBLE:", nums)
            print(
                "Đã thử reload tự động nhưng vẫn chưa đủ 0-9. "
                "Giữ browser mở để kiểm tra giao diện."
            )

        print("Tool không click số và không đặt cược.")
        input("Nhấn ENTER để đóng browser...")
        ctx.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    pcheck = sub.add_parser("check")
    pcheck.add_argument("--headless", action="store_true")

    sub.add_parser("show-numbers")

    args = ap.parse_args()

    if args.cmd == "init":
        init_profile()
    elif args.cmd == "check":
        check_profile(args.headless)
    elif args.cmd == "show-numbers":
        show_numbers()


if __name__ == "__main__":
    main()
