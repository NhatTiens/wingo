"""Offline real-bet regression tests. Never connects to 88i or sends money."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from wingo_realbet import RealBetError, RealBetExecutor
import wingo_id77_adaptive_v7_6 as app
from reconcile_real_bet import reconcile


class FakeExecutor:
    def __init__(self, *, on_prepare=None, dry_run=False, balance=1000000):
        self.calls = []
        self.on_prepare = on_prepare
        self.dry_run = dry_run
        self.balance = balance
        self.previous_win = None

    def configured(self):
        return True, "OK"

    def read_balance(self):
        return float(self.balance)

    def place_top7(self, issue, numbers, stake, total, before_submit=None, previous_win=None):
        self.calls.append((issue, numbers, stake, total))
        self.previous_win = previous_win
        if self.on_prepare:
            self.on_prepare()
        before_submit()
        return {"ok": True, "confirmed": True, "dry_run": self.dry_run,
                "balance_after": self.balance - total, "ticket_text": "SIMULATED"}


def args():
    return SimpleNamespace(
        real_bet_enabled=True, real_stop_consecutive_losses=5,
        real_stop_profit_pct=1.0, real_stop_balance_floor_pct=.50,
        real_stop_max_minutes=90, real_base_stake=2000,
    )


def gate(**changes):
    base = {"passed": True, "failed": [], "data_health": "OK", "calibrated_prob": .72,
            "consensus_score": .55, "stability_score": .75, "drift_score": .45,
            "live_samples": 80, "live_hit_rate": .72, "regime_samples": 50,
            "regime_hit_rate": .72, "selected": [0, 1, 2, 3, 4, 5, 6]}
    return dict(base, **changes)


class RealWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = app.init_db(":memory:", 525000, 3000)
        self.conn.execute("UPDATE runtime_control SET tool_running=1,real_bet_armed=1,status='START_REQUESTED' WHERE id=1")
        self.conn.commit()
        self.a = args()

    def tearDown(self):
        self.conn.close()

    def test_only_seven_number_bet_placed_once_and_settled(self):
        executor = FakeExecutor()
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor), "PLACED")
        self.assertEqual(executor.calls, [("123", [0, 1, 2, 3, 4, 5, 6], 2000.0, 14000.0)])
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor), "EXISTS")
        self.assertEqual(len(executor.calls), 1)
        row = self.conn.execute("SELECT selected_numbers_json,total_stake,status FROM real_bets").fetchone()
        self.assertEqual(json.loads(row[0]), list(range(7)))
        self.assertEqual(row[1:], (14000, "OPEN"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)
        app.settle_real_bet(self.conn, "123", 3, self.a)
        self.assertEqual(self.conn.execute("SELECT status FROM real_bets").fetchone()[0], "WIN")
        self.assertEqual(app.runtime_control(self.conn)["current_step"], 0)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "124", gate(), self.a, executor), "PLACED")
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(executor.calls[-1][2:], (2000.0, 14000.0))

    def test_win_uses_drawn_number_without_waiting_for_payout(self):
        executor = FakeExecutor(balance=220265)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "642", gate(), self.a, executor), "PLACED")
        self.assertEqual(app.runtime_control(self.conn)["current_balance"], 206265)
        app.settle_real_bet(self.conn, "642", 3, self.a)
        row = self.conn.execute("SELECT status,actual_number,payout,profit FROM real_bets WHERE issue='642'").fetchone()
        self.assertEqual(row, ("WIN", 3, 19790, 5790))
        self.assertEqual(app.runtime_control(self.conn)["current_balance"], 206265)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "643", gate(), self.a, executor), "PLACED")
        self.assertEqual(executor.previous_win, (19790.0, 206265.0))

    def test_unknown_records_draw_without_counting_unverified_winnings(self):
        self.conn.execute("""INSERT INTO real_bets(issue,step_index,unit_multiplier,selected_numbers_json,
            stake_per_number,total_stake,odds,status,created_at)
            VALUES('643',0,1,'[0, 4, 3, 6, 5, 9, 1]',2000,14000,9.895,'UNKNOWN','now')""")
        self.conn.commit()
        app.settle_real_bet(self.conn, "643", 4, self.a)
        self.assertEqual(self.conn.execute("SELECT status,actual_number,profit FROM real_bets WHERE issue='643'").fetchone(),
                         ("UNKNOWN", 4, None))
        self.assertEqual(app.runtime_control(self.conn)["session_bets"], 0)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "644", gate(), self.a, FakeExecutor()), "WAIT_OPEN_BET")

    def test_losses_reach_ten_thousand_and_stay_there_until_win(self):
        executor = FakeExecutor()
        for issue, stake, total, next_step in [
            ("101", 2000, 14000, 1),
            ("102", 6000, 42000, 2),
            ("103", 8000, 56000, 3),
            ("104", 10000, 70000, 3),
            ("105", 10000, 70000, 3),
        ]:
            self.assertEqual(app.maybe_place_real_bet(self.conn, issue, gate(), self.a, executor), "PLACED")
            self.assertEqual(executor.calls[-1][2:], (float(stake), float(total)))
            app.settle_real_bet(self.conn, issue, 9, self.a)
            self.assertEqual(app.runtime_control(self.conn)["current_step"], next_step)
        self.assertEqual(app.runtime_control(self.conn)["current_loss_streak"], 5)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "106", gate(), self.a, executor), "PLACED")
        self.assertEqual(executor.calls[-1][2:], (10000.0, 70000.0))
        app.settle_real_bet(self.conn, "106", 3, self.a)
        self.assertEqual(app.runtime_control(self.conn)["current_step"], 0)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "107", gate(), self.a, executor), "PLACED")
        self.assertEqual(executor.calls[-1][2:], (2000.0, 14000.0))
        self.assertEqual(app.runtime_control(self.conn)["real_bet_armed"], 1)

    def test_win_after_loss_resets_next_stake_to_base(self):
        executor = FakeExecutor()
        self.assertEqual(app.maybe_place_real_bet(self.conn, "201", gate(), self.a, executor), "PLACED")
        app.settle_real_bet(self.conn, "201", 9, self.a)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "202", gate(), self.a, executor), "PLACED")
        self.assertEqual(executor.calls[-1][2:], (6000.0, 42000.0))
        app.settle_real_bet(self.conn, "202", 3, self.a)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "203", gate(), self.a, executor), "PLACED")
        self.assertEqual(executor.calls[-1][2:], (2000.0, 14000.0))
        self.assertEqual(app.runtime_control(self.conn)["current_loss_streak"], 0)

    def test_insufficient_balance_reports_amount_without_submit(self):
        executor = FakeExecutor(balance=69000)
        for issue in ("301", "302", "303"):
            self.assertEqual(app.maybe_place_real_bet(self.conn, issue, gate(), self.a, executor), "PLACED")
            app.settle_real_bet(self.conn, issue, 9, self.a)
        self.assertEqual(app.runtime_control(self.conn)["current_step"], 3)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "304", gate(), self.a, executor), "INSUFFICIENT_BALANCE")
        self.assertEqual(len(executor.calls), 3)
        ctl=app.runtime_control(self.conn)
        self.assertEqual(ctl["real_bet_armed"], 0)
        self.assertEqual(ctl["stop_reason"], "INSUFFICIENT_BALANCE")
        self.assertIn("thiếu 1.000đ",ctl["last_error"])
        self.assertEqual(executor.calls[-1][2:], (8000.0, 56000.0))

    def test_exact_required_balance_allows_fourth_step(self):
        executor = FakeExecutor(balance=70000)
        for issue in ("401", "402", "403"):
            self.assertEqual(app.maybe_place_real_bet(self.conn, issue, gate(), self.a, executor), "PLACED")
            app.settle_real_bet(self.conn, issue, 9, self.a)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "404", gate(), self.a, executor), "PLACED")
        self.assertEqual(executor.calls[-1][2:], (10000.0, 70000.0))

    def test_real_decision_matches_actual_top7_gate_at_boundaries(self):
        top_args = SimpleNamespace(
            top7_calibration_lookback=500, top7_consensus_fraction=.70,
            top7_live_lookback=80, top7_regime_lookback=80,
            top7_calibrated_min=.72, top7_consensus_min=.55,
            top7_stability_min=.75, top7_max_drift=.45,
            top7_live_activate_samples=80, top7_live_min_hit=.72,
            top7_regime_activate_samples=50, top7_regime_min_hit=.72,
            top7_max_ece=.06,
        )
        with (patch.object(app, "top7_selection", return_value=(list(range(7)), [7, 8, 9], .72, 0)),
              patch.object(app, "top7_calibration", return_value=(.72, None, 0, 0)),
              patch.object(app, "top7_consensus", return_value=(.55, 2, {}, 1)),
              patch.object(app, "top7_stability", return_value=(.75, {})),
              patch.object(app, "top7_adaptive_hit_stats", side_effect=[
                  {"n": 80, "hit_rate": .72}, {"n": 50, "hit_rate": .72},
                  {"n": 80, "hit_rate": .72}, {"n": 49, "hit_rate": .70}])):
            actual_gate = app.top7_gate(self.conn, np.ones(10) / 10, {}, np.array([0]),
                                        "BALANCED", "OK", .45, top_args)
            warmup_gate = app.top7_gate(self.conn, np.ones(10) / 10, {}, np.array([0]),
                                        "BALANCED", "OK", .45, top_args)
        self.assertTrue(actual_gate["passed"])
        self.assertEqual(app.real_gate_check(actual_gate), (True, []))
        self.assertEqual(actual_gate["statuses"]["regime_hit_ok"],"PASS")
        self.assertEqual(warmup_gate["statuses"]["regime_hit_ok"],"WARMUP")
        actual_gate["passed"] = False
        actual_gate["failed"] = ["live_hit_ok"]
        self.assertEqual(app.real_gate_check(actual_gate), (False, ["live_hit_ok"]))

    def test_failed_gate_never_calls_executor(self):
        executor = FakeExecutor()
        status = app.maybe_place_real_bet(self.conn, "123", gate(passed=False, failed=["consensus_ok"], consensus_score=.5), self.a, executor)
        self.assertEqual(status, "TOP7_GATE_SKIP:consensus_ok")
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM real_bets").fetchone()[0], 0)

    def test_live_hit_gate_blocks_after_required_samples(self):
        executor = FakeExecutor()
        status = app.maybe_place_real_bet(self.conn, "123", gate(passed=False, failed=["live_hit_ok"], live_hit_rate=.70), self.a, executor)
        self.assertEqual(status, "TOP7_GATE_SKIP:live_hit_ok")
        self.assertEqual(executor.calls, [])

    def test_warmup_gate_passes(self):
        executor = FakeExecutor()
        warmup = gate(live_samples=0, live_hit_rate=None, regime_samples=0, regime_hit_rate=None)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", warmup, self.a, executor), "PLACED")

    def test_legacy_stop_thresholds_do_not_interrupt_real_betting(self):
        self.conn.execute("""UPDATE runtime_control SET current_loss_streak=20,
            session_start_balance=50000,stop_consecutive_losses=5,stop_profit_pct=1,
            stop_balance_floor_pct=.5,stop_max_minutes=1,started_at='2000-01-01T00:00:00+00:00'
            WHERE id=1""")
        self.conn.commit()
        executor = FakeExecutor()
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor), "PLACED")
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(app.runtime_control(self.conn)["real_bet_armed"], 1)

    def test_end_during_preparation_prevents_submit(self):
        def end():
            self.conn.execute("UPDATE runtime_control SET tool_running=0,real_bet_armed=0 WHERE id=1")
            self.conn.commit()
        executor = FakeExecutor(on_prepare=end)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor), "ERROR")
        self.assertEqual(self.conn.execute("SELECT status FROM real_bets").fetchone()[0], "ERROR")

    def test_issue_guard_prevents_submit(self):
        executor = FakeExecutor()
        def stale():
            raise RealBetError("wrong issue")
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor,
                                                  submit_guard=stale), "ERROR")

    def test_dry_run_does_not_create_open_bet(self):
        executor = FakeExecutor(dry_run=True)
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor), "DRY_RUN")
        self.assertEqual(self.conn.execute("SELECT status FROM real_bets").fetchone()[0], "DRY_RUN")
        self.assertEqual(app.runtime_control(self.conn)["real_bet_armed"], 0)


class Top7AdaptiveHitTests(unittest.TestCase):
    def setUp(self):
        self.conn=app.init_db(":memory:",525000,3000)

    def tearDown(self):
        self.conn.close()

    def prediction(self,issue,hit,regime="BALANCED"):
        self.conn.execute("""INSERT INTO top7_predictions(issue,created_at,selected_numbers_json,
            hit_probability,break_even_probability,estimated_roi,regime_label,hit)
            VALUES(?,?,?,?,?,?,?,?)""",
            (str(issue),"now",json.dumps(list(range(7))),.75,.71,.05,regime,hit))
        self.conn.commit()

    def test_regime_and_live_retain_80_while_replacing_opposite_result(self):
        for issue in range(1,81):
            self.prediction(issue,1 if issue<=53 else 0)
        live=app.top7_adaptive_hit_stats(self.conn,80)
        regime=app.top7_adaptive_hit_stats(self.conn,80,"BALANCED")
        self.assertEqual((live["n"],live["hit_rate"]),(80,53/80))
        self.assertEqual(regime,live)

        self.prediction(81,None)
        app.settle_top7(self.conn,"81",3)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80)["hit_rate"],54/80)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80,"BALANCED")["hit_rate"],54/80)

        self.prediction(82,None)
        app.settle_top7(self.conn,"82",9)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80)["hit_rate"],53/80)
        self.prediction(83,None)
        app.settle_top7(self.conn,"83",9)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80,"BALANCED")["hit_rate"],52/80)
        app.settle_top7(self.conn,"83",9)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80)["hit_rate"],52/80)

        self.prediction(84,None,"NEW_REGIME")
        app.settle_top7(self.conn,"84",3)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80)["hit_rate"],53/80)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80,"BALANCED")["hit_rate"],52/80)
        self.assertEqual(app.top7_adaptive_hit_stats(self.conn,80,"NEW_REGIME"),
                         {"n":1,"hit_rate":1.0})

    def test_regime_reaches_fifty_before_full_window(self):
        for issue in range(1,51):
            self.prediction(issue,1 if issue<=36 else 0,"NEW_REGIME")
        stats=app.top7_adaptive_hit_stats(self.conn,80,"NEW_REGIME")
        self.assertEqual((stats["n"],stats["hit_rate"]),(50,36/50))
        self.prediction(51,None,"NEW_REGIME")
        app.settle_top7(self.conn,"51",3)
        stats=app.top7_adaptive_hit_stats(self.conn,80,"NEW_REGIME")
        self.assertEqual((stats["n"],stats["hit_rate"]),(50,37/50))

    def test_empty_history_and_saved_counter_survive_reopening_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/"adaptive.db"
            conn=app.init_db(db,525000,3000)
            try:
                conn.execute("""INSERT INTO top7_predictions(issue,created_at,selected_numbers_json,
                    hit_probability,break_even_probability,estimated_roi,regime_label)
                    VALUES(?,?,?,?,?,?,?)""",("1","now",json.dumps(list(range(7))),.75,.71,.05,"R"))
                conn.commit()
                app.settle_top7(conn,"1",3)
                self.assertEqual(app.top7_adaptive_hit_stats(conn,80),{"n":1,"hit_rate":1.0})
                conn.commit()
            finally:
                conn.close()
            conn=app.init_db(db,525000,3000)
            try:
                self.assertEqual(app.top7_adaptive_hit_stats(conn,80),{"n":1,"hit_rate":1.0})
                self.assertEqual(app.top7_adaptive_hit_stats(conn,80,"R"),{"n":1,"hit_rate":1.0})
            finally:
                conn.close()


class ReconciliationTests(unittest.TestCase):
    def test_verified_missing_order_can_be_closed_without_creating_a_bet(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = app.init_db(db, 525000, 3000)
            try:
                conn.execute("INSERT INTO real_bets(issue,step_index,unit_multiplier,selected_numbers_json,"
                             "stake_per_number,total_stake,odds,status,created_at) "
                             "VALUES('643',0,1,'[0, 4, 3, 6, 5, 9, 1]',2000,14000,9.895,'UNKNOWN','now')")
                conn.commit()
                with self.assertRaisesRegex(ValueError, "Tổng cược"):
                    reconcile(db, "643", expected_numbers=[0, 4, 3, 6, 5, 9, 1],
                              expected_total=21000, mark_not_placed=True)
                self.assertEqual(reconcile(db, "643", expected_numbers=[0, 4, 3, 6, 5, 9, 1],
                                           expected_total=14000, mark_not_placed=True), "NOT_PLACED")
                self.assertEqual(conn.execute("SELECT status FROM real_bets WHERE issue='643'").fetchone()[0], "NOT_PLACED")
                self.assertEqual(app.runtime_control(conn)["session_bets"], 0)
                conn.execute("UPDATE runtime_control SET tool_running=1,real_bet_armed=1 WHERE id=1")
                conn.commit()
                self.assertEqual(app.maybe_place_real_bet(conn, "644", gate(), args(), FakeExecutor()), "PLACED")
            finally:
                conn.close()

    def test_confirmed_unknown_settles_once_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = app.init_db(db, 525000, 3000)
            conn.execute("INSERT INTO real_bets(issue,step_index,unit_multiplier,selected_numbers_json,"
                         "stake_per_number,total_stake,odds,status,created_at) "
                         "VALUES('123',0,1,'[0, 1, 2, 3, 4, 5, 6]',3000,21000,9.895,'UNKNOWN','now')")
            conn.execute("INSERT INTO results VALUES('123',3,'small','green','now','offline')")
            conn.commit()
            self.assertEqual(reconcile(db, "123"), "UNKNOWN")
            with self.assertRaisesRegex(ValueError, "Tổng cược"):
                reconcile(db, "123", True, list(range(7)), 7000)
            real_connect = sqlite3.connect
            connections = []
            def tracked_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connections.append(connection)
                return connection
            with patch("reconcile_real_bet.sqlite3.connect", side_effect=tracked_connect):
                self.assertEqual(reconcile(db, "123", True, list(range(7)), 21000), "WIN")
            self.assertEqual(len(connections), 2)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            self.assertEqual(conn.execute("SELECT status FROM real_bets WHERE issue='123'").fetchone()[0], "WIN")
            self.assertEqual(app.runtime_control(conn)["session_bets"], 1)
            self.assertEqual(app.runtime_control(conn)["current_step"], 0)
            self.assertTrue((Path(tmp) / "test.before-reconcile-123.db").is_file())
            with self.assertRaisesRegex(ValueError, "Chỉ đối soát"):
                reconcile(db, "123", True, list(range(7)), 21000)
            conn.close()


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    def count(self):
        if self.selector == "selected":
            return self.page.selected
        if self.selector == "rows":
            return 7
        return 1

    def is_visible(self):
        return True

    def click(self):
        if self.selector == "clear":
            if not self.page.clear_broken:
                self.page.selected = 0
        elif self.selector.startswith("num_"):
            self.page.selected += 1
        elif self.selector == "submit":
            self.page.submit_count += 1
            if self.page.click_timeout:
                raise TimeoutError("click timed out after dispatch")
            self.page.balance += self.page.submit_payout - self.page.submit_debit

    def fill(self, value):
        self.page.filled = value

    def press(self, key):
        pass

    def scroll_into_view_if_needed(self, **kwargs):
        pass


class Page:
    def __init__(self):
        self.selected = 0
        self.submit_count = 0
        self.balance = 1000
        self.submit_debit = 70
        self.submit_payout = 0
        self.clear_broken = False
        self.click_timeout = False
        self.stale_until_reload = False
        self.refresh_count = 0
        self.wait_count = 0
        self.close_after_submit = False

    def locator(self, selector):
        return Locator(self, selector)

    def wait_for_timeout(self, ms):
        if self.close_after_submit and self.submit_count:
            raise RuntimeError("page closed")
        self.wait_count += 1

    def reload(self, **kwargs):
        self.refresh_count += 1
        self.stale_until_reload = False

    def bring_to_front(self):
        pass


class OfflineExecutor(RealBetExecutor):
    def __init__(self, config, page):
        super().__init__(config, "unused-profile")
        self.cfg = json.loads(Path(config).read_text())
        self.fake_page = page

    def _goto(self, force=False):
        return self.fake_page

    def read_balance(self):
        if self.fake_page.submit_count:
            raise AssertionError("Không được điều hướng bằng read_balance() sau submit")
        return self.fake_page.balance

    def _balance_visible_value(self):
        if self.fake_page.stale_until_reload:
            return self.fake_page.balance + 70
        return self.fake_page.balance

    def ensure_number_panel(self):
        return list(range(10))

    def _verify_row_stakes(self, page, expected):
        return [expected] * 7

    def _read_total(self, page):
        return 70


class BrowserFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.path.write_text(json.dumps({
            "enabled": True, "dry_run": False,
            "selection": {"selected_selector": "selected", "clear_selector": "clear",
                          "number_button_selector_template": "num_{n}"},
            "stake": {"shared_input_selector": "shared"},
            "submit": {"selector": "submit"}, "total": {"selector": "total"},
            "success": {"mode": "balance_decrease", "timeout_ms": 100, "refresh_after_ms": 0}
        }))
        self.page = Page()
        self.executor = OfflineExecutor(self.path, self.page)

    def test_exact_seven_numbers_and_pre_submit_guard(self):
        guard_calls = []
        result = self.executor.place_top7("123", list(range(7)), 10, 70,
                                          before_submit=lambda: guard_calls.append(1))
        self.assertTrue(result["confirmed"])
        self.assertEqual((self.page.selected, self.page.submit_count, guard_calls), (7, 1, [1]))

    def test_old_cart_must_clear(self):
        self.page.selected = 1
        self.page.clear_broken = True
        with self.assertRaisesRegex(RealBetError, "vẫn còn lựa chọn"):
            self.executor.place_top7("123", list(range(7)), 10, 70)
        self.assertEqual(self.page.submit_count, 0)

    def test_stale_balance_refreshes_once_and_confirms_without_second_submit(self):
        self.page.stale_until_reload = True
        result = self.executor.place_top7("123", list(range(7)), 10, 70)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["balance_after"], 930)
        self.assertEqual((self.page.refresh_count, self.page.submit_count), (1, 1))

    def test_no_exact_debit_keeps_submission_unknown(self):
        self.page.submit_debit = 50
        self.page.stale_until_reload = True
        with self.assertRaises(RealBetError) as result:
            self.executor.place_top7("123", list(range(7)), 10, 70)
        self.assertTrue(result.exception.may_have_submitted)
        self.assertEqual(self.page.submit_count, 1)

    def test_previous_win_payout_overlapping_debit_confirms_once(self):
        self.page.submit_payout = 99
        result = self.executor.place_top7("124", list(range(7)), 10, 70,
                                          previous_win=(99, 1000))
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["balance_after"], 1029)
        self.assertIn("BALANCE_NET_WITH_PREVIOUS_WIN", result["ticket_text"])
        self.assertEqual(self.page.submit_count, 1)

    def test_previous_win_without_new_debit_stays_unknown(self):
        self.page.submit_payout = 99
        self.page.submit_debit = 0
        with self.assertRaises(RealBetError) as result:
            self.executor.place_top7("124", list(range(7)), 10, 70,
                                     previous_win=(99, 1000))
        self.assertTrue(result.exception.may_have_submitted)
        self.assertEqual(self.page.submit_count, 1)

    def test_wrong_prior_balance_cannot_confirm_net_change(self):
        self.page.submit_payout = 99
        with self.assertRaises(RealBetError) as result:
            self.executor.place_top7("124", list(range(7)), 10, 70,
                                     previous_win=(99, 1010))
        self.assertTrue(result.exception.may_have_submitted)

    def test_page_closes_after_submit_is_unknown_without_retry(self):
        self.page.close_after_submit = True
        with self.assertRaises(RealBetError) as result:
            self.executor.place_top7("123", list(range(7)), 10, 70)
        self.assertTrue(result.exception.may_have_submitted)
        self.assertEqual(self.page.submit_count, 1)

    def test_out_of_range_number_rejected(self):
        with self.assertRaisesRegex(RealBetError, "TOP7 không hợp lệ"):
            self.executor.place_top7("123", [0, 1, 2, 3, 4, 5, 10], 10, 70)
        self.assertEqual(self.page.submit_count, 0)

    def test_click_timeout_is_uncertain_submission(self):
        self.page.click_timeout = True
        with self.assertRaises(RealBetError) as result:
            self.executor.place_top7("123", list(range(7)), 10, 70)
        self.assertTrue(result.exception.may_have_submitted)
        self.assertEqual(self.page.submit_count, 1)

    def test_repo_config_is_ready_and_wrong_game_rejected(self):
        repo_config = Path(__file__).with_name("88i_realbet_config.json")
        ready, reason = RealBetExecutor(repo_config, "unused-profile").configured()
        self.assertTrue(ready, reason)
        cfg = json.loads(repo_config.read_text())
        cfg["target_url"] = cfg["target_url"].replace("id=77", "id=78")
        self.path.write_text(json.dumps(cfg))
        ready, reason = RealBetExecutor(self.path, "unused-profile").configured()
        self.assertFalse(ready)
        self.assertIn("id=77", reason)


if __name__ == "__main__":
    unittest.main()
