"""Reconcile one UNKNOWN real bet after checking that exact order on 88i.

Read-only unless --mark-placed or --mark-not-placed is supplied. Run with the writer stopped.
"""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


def reconcile(db, issue, mark_placed=False, expected_numbers=None, expected_total=None,
              mark_not_placed=False):
    path = Path(db)
    if not path.is_file():
        raise ValueError(f"Không tìm thấy DB: {path}")
    with closing(sqlite3.connect(path, timeout=10)) as conn:
        row = conn.execute(
            "SELECT selected_numbers_json,stake_per_number,total_stake,status,balance_before,error_text "
            "FROM real_bets WHERE issue=?", (str(issue),)
        ).fetchone()
        if row is None:
            raise ValueError(f"Không có lệnh issue={issue} trong DB")
        numbers = json.loads(row[0])
        print(f"Issue: {issue} | số: {numbers} | cược mỗi số: {row[1]:,.0f} | "
              f"tổng: {row[2]:,.0f} | trạng thái: {row[3]} | số dư trước: {row[4]}")
        draw = conn.execute("SELECT number FROM results WHERE issue=?", (str(issue),)).fetchone()
        if draw:
            print(f"Số mở thưởng: {draw[0]} | dãy số: {'Trúng' if int(draw[0]) in numbers else 'Trượt'}")
        if row[5]:
            print(f"Lỗi xác nhận lệnh: {row[5]}")
        if mark_placed and mark_not_placed:
            raise ValueError("Chỉ được chọn một cách đối soát")
        if not mark_placed and not mark_not_placed:
            return row[3]

        if expected_numbers is None or expected_total is None:
            raise ValueError("Cần --expected-numbers và --expected-total để xác nhận đúng lệnh")
        if row[3] != "UNKNOWN":
            raise ValueError(f"Chỉ đối soát trạng thái UNKNOWN, hiện là {row[3]}")
        if numbers != expected_numbers or len(numbers) != 7 or len(set(numbers)) != 7:
            raise ValueError("7 số trong DB không khớp lệnh đã kiểm tra trên 88i")
        if abs(float(row[2]) - float(expected_total)) > 0.01:
            raise ValueError("Tổng cược trong DB không khớp lệnh đã kiểm tra trên 88i")
        if conn.execute("SELECT tool_running FROM runtime_control WHERE id=1").fetchone()[0]:
            raise ValueError("Dừng writer trước khi đối soát DB")

        backup_path = path.with_name(f"{path.stem}.before-reconcile-{issue}.db")
        if backup_path.exists():
            raise ValueError(f"Đã có file backup {backup_path}; không ghi đè")
        with closing(sqlite3.connect(backup_path)) as backup:
            conn.backup(backup)
        conn.execute("BEGIN IMMEDIATE")
        if mark_not_placed:
            updated = conn.execute(
                "UPDATE real_bets SET status='NOT_PLACED',resolved_at=?,"
                "error_text=COALESCE(error_text,'') || ' | MANUALLY_VERIFIED_NOT_PLACED_88I' "
                "WHERE issue=? AND status='UNKNOWN'",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), str(issue)),
            )
            if updated.rowcount != 1:
                raise ValueError("Trạng thái lệnh đã thay đổi; chưa cập nhật")
            conn.commit()
            print(f"Đã đối soát: NOT_PLACED | backup: {backup_path}")
            return "NOT_PLACED"
        updated = conn.execute(
            "UPDATE real_bets SET status='OPEN',ticket_text=?,error_text=NULL "
            "WHERE issue=? AND status='UNKNOWN'",
            ("MANUALLY_VERIFIED_88I", str(issue)),
        )
        if updated.rowcount != 1:
            raise ValueError("Trạng thái lệnh đã thay đổi; chưa cập nhật")
        known = conn.execute("SELECT number FROM results WHERE issue=?", (str(issue),)).fetchone()
        if known:
            # The same settlement logic used by the writer updates the session
            # progression and accounting, using the current real-bet rule.
            import wingo_id77_adaptive_v7_6 as app
            ctl_args = SimpleNamespace(real_stop_consecutive_losses=5,
                                       real_stop_profit_pct=1.0,
                                       real_stop_balance_floor_pct=.50,
                                       real_stop_max_minutes=90)
            app.settle_real_bet(conn, str(issue), int(known[0]), ctl_args)
        else:
            conn.commit()
        status = conn.execute("SELECT status FROM real_bets WHERE issue=?", (str(issue),)).fetchone()[0]
        print(f"Đã đối soát: {status} | backup: {backup_path}")
        return status


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="wingo_88i_id77.db")
    ap.add_argument("--issue", required=True)
    ap.add_argument("--mark-placed", action="store_true",
                    help="Chỉ dùng sau khi xác nhận đúng issue, 7 số và tổng cược ở lịch sử 88i")
    ap.add_argument("--mark-not-placed", action="store_true",
                    help="Chỉ dùng sau khi xác nhận 88i KHÔNG nhận lệnh đúng issue, 7 số và tổng cược")
    ap.add_argument("--expected-numbers", help="Bảy số đúng thứ tự trong DB, ví dụ 4,9,8,7,6,1,3")
    ap.add_argument("--expected-total", type=float)
    a = ap.parse_args()
    try:
        expected_numbers = [int(s) for s in a.expected_numbers.split(",")] if a.expected_numbers else None
        reconcile(a.db, a.issue, a.mark_placed, expected_numbers, a.expected_total, a.mark_not_placed)
    except (ValueError, sqlite3.Error) as exc:
        ap.exit(2, f"Không đối soát: {exc}\n")


if __name__ == "__main__":
    main()
