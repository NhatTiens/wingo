#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable


class RealBetError(RuntimeError):
    def __init__(self, message: str, *, may_have_submitted: bool = False):
        super().__init__(message)
        self.may_have_submitted = bool(may_have_submitted)


def parse_money(text: str) -> float:
    if text is None:
        raise RealBetError("Không đọc được text số tiền")
    s = str(text).strip().replace("\u00a0", " ")
    nums = re.findall(r"\d[\d.,\s]*", s)
    if not nums:
        raise RealBetError(f"Không parse được số tiền từ: {s[:120]!r}")
    cand = max(nums, key=lambda x: len(re.sub(r"\D", "", x)))
    digits = re.sub(r"\D", "", cand)
    if not digits:
        raise RealBetError(f"Không parse được số tiền từ: {s[:120]!r}")
    return float(int(digits))


def load_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise RealBetError(f"Thiếu config real bet: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RealBetError(f"Config JSON lỗi: {exc}") from exc


class RealBetExecutor:
    """88idd executor dùng persistent Chromium profile.

    Các nguyên tắc fail-closed:
    - Không thấy balance -> STOP.
    - Không render đủ 0..9 -> reload tối đa N lần, vẫn thiếu -> STOP.
    - Không đúng 7 input stake / tổng tiền -> STOP trước submit.
    - dry_run=true -> tuyệt đối không click submit.
    - Sau submit thật, mặc định xác nhận bằng biến động số dư; nếu không xác nhận được -> UNKNOWN + STOP.
    """

    def __init__(self, config_path: str | Path, profile_dir: str | Path):
        self.config_path = Path(config_path)
        self.profile_dir = Path(profile_dir)
        self.cfg: dict[str, Any] | None = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def reload_config(self) -> dict[str, Any]:
        self.cfg = load_config(self.config_path)
        return self.cfg

    def configured(self) -> tuple[bool, str]:
        try:
            cfg = self.reload_config()
        except Exception as exc:
            return False, str(exc)
        if not bool(cfg.get("enabled", False)):
            return False, "88i_realbet_config.json đang enabled=false"

        login_cfg = cfg.get("login") or {}
        login_mode = str(login_cfg.get("mode") or "manual_each_run").strip().lower()
        if login_mode not in {"manual_each_run", "persistent"}:
            return False, f"login.mode không hợp lệ: {login_mode}"
        if login_mode == "persistent" and not self.profile_dir.exists():
            return False, f"Thiếu persistent profile: {self.profile_dir}"
        if login_mode == "manual_each_run" and bool(cfg.get("headless", False)):
            return False, "manual_each_run yêu cầu headless=false để người dùng đăng nhập trên Chromium"
        if str(cfg.get("target_url") or "") != "https://www.88idd.com/home/#/lottery?tabName=Lottery&id=77":
            return False, "target_url phải là trang Wingo id=77 đã kiểm tra"
        if str((cfg.get("selection") or {}).get("expected_selected_count", "")) != "7":
            return False, "expected_selected_count phải bằng 7"
        if str((cfg.get("stake") or {}).get("expected_row_inputs", "")) != "7":
            return False, "expected_row_inputs phải bằng 7"
        if str((cfg.get("success") or {}).get("mode") or "") != "balance_decrease":
            return False, "Xác nhận cược thật phải dùng balance_decrease"

        required = {
            "balance.selector": (cfg.get("balance") or {}).get("selector"),
            "selection.number_button_selector_template": (cfg.get("selection") or {}).get("number_button_selector_template"),
            "selection.selected_selector": (cfg.get("selection") or {}).get("selected_selector"),
            "selection.clear_selector": (cfg.get("selection") or {}).get("clear_selector"),
            "stake.shared_input_selector": (cfg.get("stake") or {}).get("shared_input_selector"),
            "stake.row_input_selector": (cfg.get("stake") or {}).get("row_input_selector"),
            "total.selector": (cfg.get("total") or {}).get("selector"),
            "submit.selector": (cfg.get("submit") or {}).get("selector"),
        }
        missing = [k for k, v in required.items() if not str(v or "").strip()]
        if missing:
            return False, "Thiếu selector: " + ", ".join(missing)
        return True, "OK"

    def _ensure(self):
        ok, reason = self.configured()
        if not ok:
            raise RealBetError(reason)
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RealBetError(
                "Chưa cài Playwright. Chạy: pip install playwright && python -m playwright install chromium"
            ) from exc

        self._pw = sync_playwright().start()
        headless = bool(self.cfg.get("headless", False))
        login_cfg = self.cfg.get("login") or {}
        login_mode = str(login_cfg.get("mode") or "manual_each_run").strip().lower()

        if login_mode == "manual_each_run":
            # Chromium sạch cho mỗi lần chạy writer: không tái sử dụng cookie đăng nhập cũ.
            # Người dùng tự nhập tài khoản/mật khẩu trực tiếp trên website; tool không đọc/lưu password.
            self._browser = self._pw.chromium.launch(headless=False)
            self._context = self._browser.new_context(
                viewport={"width": 1600, "height": 1000},
            )
            self._page = self._context.new_page()
        else:
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=headless,
                viewport={"width": 1600, "height": 1000},
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        self._page.set_default_timeout(int(self.cfg.get("default_timeout_ms", 8000)))

    @property
    def page(self):
        self._ensure()
        return self._page

    def _balance_visible_value(self):
        page = self.page
        sel = str((self.cfg.get("balance") or {}).get("selector") or "").strip()
        if not sel:
            return None
        try:
            loc = page.locator(sel)
            if loc.count() != 1 or not loc.first.is_visible():
                return None
            txt = loc.first.inner_text().strip() or (loc.first.get_attribute("value") or "")
            return parse_money(txt)
        except Exception:
            return None

    def prepare_login(self) -> float:
        """Mở Chromium hiển thị và chờ người dùng đăng nhập trực tiếp trên 88idd.

        Với login.mode=manual_each_run, mỗi lần writer chạy là một browser context sạch,
        nên website sẽ yêu cầu đăng nhập lại. Tool không nhận hoặc lưu mật khẩu.
        """
        self.reload_config()
        page = self.page
        login_cfg = self.cfg.get("login") or {}
        login_mode = str(login_cfg.get("mode") or "manual_each_run").strip().lower()
        url = str(self.cfg.get("target_url") or "https://www.88idd.com/home/#/lottery?tabName=Lottery&id=77")

        page.bring_to_front()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(int((self.cfg.get("timing") or {}).get("page_ready_ms", 2200)))

        if login_mode == "persistent":
            bal = self._balance_visible_value()
            if bal is None:
                raise RealBetError("Persistent session đã hết hạn; hãy đăng nhập lại hoặc chuyển login.mode=manual_each_run")
            return float(bal)

        timeout_s = max(30, int(login_cfg.get("timeout_seconds", 600)))
        poll_ms = max(250, int(login_cfg.get("poll_ms", 500)))

        print("")
        print("=" * 72)
        print("[88I LOGIN] Chromium đang mở và sẽ luôn HIỂN THỊ trong lúc tool chạy.")
        print("[88I LOGIN] Hãy đăng nhập tài khoản trực tiếp trên website trong Chromium này.")
        print("[88I LOGIN] Tool KHÔNG đọc/lưu username hoặc password.")
        print(f"[88I LOGIN] Đang chờ login tối đa {timeout_s} giây...")
        print("=" * 72)

        deadline = time.monotonic() + timeout_s
        last_notice = 0.0
        while time.monotonic() < deadline:
            bal = self._balance_visible_value()
            if bal is not None:
                # Sau login, ép về đúng Wingo id77 và để browser ở đó.
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(int((self.cfg.get("timing") or {}).get("page_ready_ms", 2200)))
                except Exception:
                    pass
                bal2 = self._balance_visible_value()
                if bal2 is not None:
                    bal = bal2
                page.bring_to_front()
                print(f"[88I LOGIN] LOGIN OK | balance={bal:,.0f}")
                return float(bal)

            now_m = time.monotonic()
            if now_m - last_notice >= 15:
                remain = max(0, int(deadline - now_m))
                print(f"[88I LOGIN] Chưa login xong... còn khoảng {remain}s")
                last_notice = now_m
            page.wait_for_timeout(poll_ms)

        raise RealBetError("LOGIN_TIMEOUT: chưa đăng nhập thành công trong thời gian cho phép")

    def _goto(self, force=False):
        page = self.page
        url = str(self.cfg.get("target_url") or "https://www.88idd.com/home/#/lottery?tabName=Lottery&id=77")
        # A logged-in page for another lottery or another tab is not our target.
        if force or page.url != url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int((self.cfg.get("timing") or {}).get("page_ready_ms", 1800)))
        return page

    @staticmethod
    def _one(page, selector: str, label: str):
        loc = page.locator(selector)
        count = loc.count()
        if count != 1:
            raise RealBetError(f"{label}: selector phải match đúng 1 element, hiện match={count}: {selector}")
        if not loc.first.is_visible():
            raise RealBetError(f"{label}: element không visible: {selector}")
        return loc.first

    def read_balance(self) -> float:
        page = self._goto()
        sel = str((self.cfg.get("balance") or {}).get("selector") or "").strip()
        try:
            el = self._one(page, sel, "balance")
        except RealBetError as exc:
            raise RealBetError("Không thấy số dư; có thể session đã logout hoặc UI chưa load") from exc
        txt = el.inner_text().strip() or (el.get_attribute("value") or "")
        value = parse_money(txt)
        if value < 0:
            raise RealBetError("Số dư âm bất thường")
        return value

    def _visible_numbers(self, page) -> list[int]:
        template = str((self.cfg.get("selection") or {}).get("number_button_selector_template") or "")
        out: list[int] = []
        for n in range(10):
            try:
                loc = page.locator(template.format(n=n, number=n)).first
                if loc.count() and loc.is_visible():
                    out.append(n)
            except Exception:
                pass
        return out

    def ensure_number_panel(self) -> list[int]:
        page = self._goto()
        ui = self.cfg.get("ui_recovery") or {}
        attempts = max(0, int(ui.get("number_panel_reload_attempts", 3)))
        wait_ms = max(500, int(ui.get("number_panel_wait_ms", 7000)))

        for attempt in range(attempts + 1):
            deadline = time.monotonic() + wait_ms / 1000.0
            while time.monotonic() < deadline:
                nums = self._visible_numbers(page)
                if len(nums) == 10:
                    return nums
                page.wait_for_timeout(400)
            if attempt >= attempts:
                break
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int((self.cfg.get("timing") or {}).get("page_ready_ms", 1800)))
            # Nếu reload làm mất login thì balance check sẽ fail ngay.
            self.read_balance()
        raise RealBetError(f"Không thấy đủ 0-9 sau {attempts} lần reload")

    def _read_total(self, page) -> float:
        sel = str((self.cfg.get("total") or {}).get("selector") or "").strip()
        el = self._one(page, sel, "displayed total")
        txt = el.inner_text().strip() or (el.get_attribute("value") or "")
        return parse_money(txt)

    def _verify_row_stakes(self, page, expected: float):
        stake_cfg = self.cfg.get("stake") or {}
        selector = str(stake_cfg.get("row_input_selector") or "").strip()
        loc = page.locator(selector)
        expected_count = int(stake_cfg.get("expected_row_inputs", 7))
        count = loc.count()
        if count != expected_count:
            raise RealBetError(f"Stake rows mismatch: web={count}, expected={expected_count}")
        tol = float(stake_cfg.get("row_tolerance", 1.0))
        vals = []
        for i in range(count):
            el = loc.nth(i)
            if not el.is_visible():
                raise RealBetError(f"Stake row {i+1} không visible")
            v = parse_money(el.input_value())
            vals.append(v)
            if abs(v - expected) > tol:
                raise RealBetError(f"Stake row {i+1}={v:.0f}, expected={expected:.0f}")
        return vals

    def place_top7(self, issue: str, numbers: list[int], stake_per_number: float, expected_total: float,
                   before_submit: Callable[[], None] | None = None) -> dict[str, Any]:
        if (len(numbers) != 7 or any(type(x) is not int or x not in range(10) for x in numbers)
                or len(set(numbers)) != 7):
            raise RealBetError(f"TOP7 không hợp lệ: {numbers}")
        if stake_per_number <= 0 or expected_total <= 0:
            raise RealBetError("Stake không hợp lệ")
        if abs(float(stake_per_number) * 7.0 - float(expected_total)) > 1:
            raise RealBetError("expected_total không khớp stake_per_number × 7")

        page = self._goto()
        try:
            page.bring_to_front()
        except Exception:
            pass
        cfg = self.cfg
        dry_run = bool(cfg.get("dry_run", True))
        timing = cfg.get("timing") or {}
        selection = cfg.get("selection") or {}
        stake_cfg = cfg.get("stake") or {}

        balance_before = self.read_balance()
        if balance_before + 1e-9 < expected_total:
            raise RealBetError(f"Không đủ số dư: {balance_before:.0f} < {expected_total:.0f}")

        self.ensure_number_panel()

        # Xóa giỏ cũ nếu đang còn lựa chọn từ lần trước.
        clear_sel = str(selection.get("clear_selector") or "").strip()
        selected_sel = str(selection.get("selected_selector") or "").strip()
        if not selected_sel:
            raise RealBetError("Thiếu selected_selector để xác nhận giỏ chỉ chứa TOP7")
        if page.locator(selected_sel).count():
            clear_loc = page.locator(clear_sel) if clear_sel else None
            if clear_loc is None or clear_loc.count() != 1 or not clear_loc.first.is_visible():
                raise RealBetError("Giỏ đang có lựa chọn và không thể xóa an toàn")
            clear_loc.first.click()
            page.wait_for_timeout(200)
            if page.locator(selected_sel).count():
                raise RealBetError("Giỏ vẫn còn lựa chọn sau khi xóa")

        template = str(selection.get("number_button_selector_template") or "").strip()
        for n in numbers:
            selector = template.format(n=int(n), number=int(n))
            el = self._one(page, selector, f"number {n}")
            el.click()
            page.wait_for_timeout(int(timing.get("between_number_click_ms", 120)))

        count = page.locator(selected_sel).count()
        if count != 7:
            raise RealBetError(f"Selected-count mismatch: web={count}, expected=7")

        shared = self._one(page, str(stake_cfg.get("shared_input_selector") or ""), "shared stake input")
        shared.click()
        shared.fill(str(int(round(stake_per_number))))
        try:
            shared.press("Tab")
        except Exception:
            pass
        page.wait_for_timeout(int(timing.get("after_fill_ms", 700)))

        row_values = self._verify_row_stakes(page, float(stake_per_number))
        displayed_total = self._read_total(page)
        tolerance = float((cfg.get("total") or {}).get("tolerance", 1.0))
        if abs(displayed_total - expected_total) > tolerance:
            raise RealBetError(f"Tổng tiền web={displayed_total:.0f}, expected={expected_total:.0f} → từ chối submit")

        result = {
            "ok": False,
            "dry_run": dry_run,
            "submitted": False,
            "confirmed": False,
            "issue": str(issue),
            "numbers": [int(x) for x in numbers],
            "stake_per_number": float(stake_per_number),
            "row_values": row_values,
            "expected_total": float(expected_total),
            "displayed_total": displayed_total,
            "balance_before": float(balance_before),
            "balance_after": None,
            "ticket_text": None,
        }

        if dry_run:
            shot = str(cfg.get("dry_run_screenshot") or "realbet_dryrun_{issue}.png")
            try:
                shot = shot.format(issue=str(issue))
                page.screenshot(path=shot, full_page=True)
                result["dry_run_screenshot"] = shot
            except Exception as exc:
                result["dry_run_screenshot"] = None
                result["dry_run_screenshot_error"] = str(exc)
            result["ok"] = True
            return result

        # Check config again immediately before the consequential click.
        latest_cfg = load_config(self.config_path)
        if not bool(latest_cfg.get("enabled", False)) or bool(latest_cfg.get("dry_run", True)):
            raise RealBetError("Config đổi sang disabled/dry_run trước submit → hủy lệnh")
        if before_submit is not None:
            before_submit()

        submit_sel = str((cfg.get("submit") or {}).get("selector") or "").strip()
        submit = self._one(page, submit_sel, "submit")
        try:
            submit.scroll_into_view_if_needed(timeout=2000)
            page.bring_to_front()
        except Exception:
            pass
        try:
            submit.click()
        except Exception as exc:
            # Playwright can time out after dispatching the click. Treat it as an
            # uncertain submission so a later START cannot silently retry it.
            raise RealBetError("Không rõ click submit đã gửi lệnh hay chưa", may_have_submitted=True) from exc
        result["submitted"] = True
        page.wait_for_timeout(int(timing.get("after_submit_ms", 500)))

        confirm_cfg = cfg.get("confirm") or {}
        confirm_sel = str(confirm_cfg.get("selector") or "").strip()
        if confirm_sel:
            confirm = page.locator(confirm_sel)
            if confirm.count() == 1 and confirm.first.is_visible():
                confirm.first.click()
                page.wait_for_timeout(int(timing.get("after_confirm_ms", 700)))
            elif bool(confirm_cfg.get("required", False)):
                raise RealBetError("Đã click submit nhưng không tìm thấy confirm", may_have_submitted=True)

        success_cfg = cfg.get("success") or {}
        mode = str(success_cfg.get("mode") or "balance_decrease")
        timeout_ms = int(success_cfg.get("timeout_ms", 5000))
        confirmed = False
        ticket_text = None
        balance_after = None

        if mode == "balance_decrease":
            deadline = time.monotonic() + timeout_ms / 1000.0
            tol = float(success_cfg.get("balance_tolerance", 2.0))
            while time.monotonic() < deadline:
                try:
                    b = float(self.read_balance())
                    balance_after = b
                    delta = balance_before - b
                    if abs(delta - expected_total) <= tol:
                        confirmed = True
                        ticket_text = f"BALANCE_DECREASE {delta:.0f}"
                        break
                except Exception:
                    pass
                page.wait_for_timeout(250)
        else:
            success_sel = str(success_cfg.get("selector") or "").strip()
            success_text = str(success_cfg.get("text_contains") or "").strip()
            if success_sel:
                loc = page.locator(success_sel)
                try:
                    loc.first.wait_for(state="visible", timeout=timeout_ms)
                    if loc.count() >= 1:
                        ticket_text = (loc.first.inner_text() or "").strip()[:500]
                        confirmed = True
                except Exception:
                    confirmed = False
            elif success_text:
                try:
                    loc = page.get_by_text(success_text, exact=False)
                    loc.first.wait_for(state="visible", timeout=timeout_ms)
                    ticket_text = (loc.first.inner_text() or "").strip()[:500]
                    confirmed = True
                except Exception:
                    confirmed = False

        result["confirmed"] = bool(confirmed)
        result["ticket_text"] = ticket_text
        result["balance_after"] = balance_after
        if not confirmed:
            raise RealBetError("Đã click submit nhưng không xác nhận được lệnh → STOP để tránh cược trùng", may_have_submitted=True)

        result["ok"] = True
        return result

    def close(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._pw = None
