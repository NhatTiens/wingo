"""Offline real-bet regression tests. Never connects to 88i or sends money."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from wingo_realbet import RealBetError, RealBetExecutor
import wingo_id77_adaptive_v7_6 as app


class FakeExecutor:
    def __init__(self, *, on_prepare=None, dry_run=False):
        self.calls = []
        self.on_prepare = on_prepare
        self.dry_run = dry_run

    def configured(self):
        return True, "OK"

    def read_balance(self):
        return 200000.0

    def place_top7(self, issue, numbers, stake, total, before_submit=None):
        self.calls.append((issue, numbers, stake, total))
        if self.on_prepare:
            self.on_prepare()
        before_submit()
        return {"ok": True, "confirmed": True, "dry_run": self.dry_run,
                "balance_after": 200000 - total, "ticket_text": "SIMULATED"}


def args():
    return SimpleNamespace(
        real_bet_enabled=True, real_calibrated_min=.72, real_consensus_min=.60,
        real_stability_min=.80, real_max_drift=.40, real_live_required_samples=80,
        real_live_min_hit=.73, real_regime_required_samples=50,
        real_regime_min_hit=.73, real_stop_consecutive_losses=5,
        real_stop_profit_pct=1.0, real_stop_balance_floor_pct=.50,
        real_stop_max_minutes=90, real_base_stake=3000,
    )


def gate(**changes):
    base = {"passed": True, "data_health": "OK", "calibrated_prob": .75,
            "consensus_score": .65, "stability_score": .85, "drift_score": .2,
            "live_samples": 80, "live_hit_rate": .75, "regime_samples": 50,
            "regime_hit_rate": .75, "selected": [0, 1, 2, 3, 4, 5, 6]}
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
        self.assertEqual(executor.calls, [("123", [0, 1, 2, 3, 4, 5, 6], 3000.0, 21000.0)])
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor), "EXISTS")
        self.assertEqual(len(executor.calls), 1)
        row = self.conn.execute("SELECT selected_numbers_json,total_stake,status FROM real_bets").fetchone()
        self.assertEqual(json.loads(row[0]), list(range(7)))
        self.assertEqual(row[1:], (21000, "OPEN"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)
        app.settle_real_bet(self.conn, "123", 3, self.a)
        self.assertEqual(self.conn.execute("SELECT status FROM real_bets").fetchone()[0], "WIN")
        self.assertEqual(app.runtime_control(self.conn)["current_step"], 1)

    def test_failed_gate_never_calls_executor(self):
        executor = FakeExecutor()
        status = app.maybe_place_real_bet(self.conn, "123", gate(consensus_score=.5), self.a, executor)
        self.assertEqual(status, "REAL_GATE_SKIP:CONSENSUS")
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM real_bets").fetchone()[0], 0)

    def test_live_hit_gate_blocks_after_required_samples(self):
        executor = FakeExecutor()
        status = app.maybe_place_real_bet(self.conn, "123", gate(live_hit_rate=.70), self.a, executor)
        self.assertEqual(status, "REAL_GATE_SKIP:LIVE_HIT")
        self.assertEqual(executor.calls, [])

    def test_stop_loss_blocks_even_if_gate_passes(self):
        self.conn.execute("UPDATE runtime_control SET current_loss_streak=5 WHERE id=1")
        self.conn.commit()
        executor = FakeExecutor()
        self.assertEqual(app.maybe_place_real_bet(self.conn, "123", gate(), self.a, executor), "LOSS_STREAK_5")
        self.assertEqual(executor.calls, [])
        self.assertEqual(app.runtime_control(self.conn)["real_bet_armed"], 0)

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
            self.page.balance -= 70

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
        self.clear_broken = False
        self.click_timeout = False

    def locator(self, selector):
        return Locator(self, selector)

    def wait_for_timeout(self, ms):
        pass

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
            "success": {"mode": "balance_decrease", "timeout_ms": 100}
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
