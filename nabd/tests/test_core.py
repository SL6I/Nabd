"""Unit tests for the parts of Nabd that decide things: the learned laws, the safety and validity
gates, and the rules that turn evidence into hypotheses. No running store is needed.

    docker compose run --rm --no-deps nabd python -m unittest discover -s /srv/tests -p "test_*.py" -v
"""

import random
import unittest

from app import actions, core_engine, learned
from app.learned import CONFIRM, Law, Learner


def quiet_learner() -> Learner:
    """A learner that touches neither disk nor Docker."""
    lr = Learner.__new__(Learner)
    lr.state, lr.rows, lr.laws, lr.flows, lr.signatures = "learning", [], [], [], {}
    lr.learned_at, lr.train_started, lr.enabled, lr.latest = None, None, True, {}
    from collections import deque
    lr.snaps = deque(maxlen=64)
    lr._save = lambda: None
    return lr


X, Y = "http|inventory|/reserve", "db|products|update"


class LearnedLawTests(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        self._sig = learned.log_signatures
        learned.log_signatures = lambda service, since: {}

    def tearDown(self):
        learned.log_signatures = self._sig

    def test_learns_a_structural_ratio_and_ignores_a_poller(self):
        lr = quiet_learner()
        for _ in range(48):
            n = random.randint(140, 160)
            lr.rows.append({"http|inventory|/reserve": n,
                            "db|products|update": n + random.choice((-1, 0, 1)),
                            "redis|set": 2 * n + random.choice((-2, 0, 2)),
                            "http|psp|/v1/supplier/catalog": 30 + random.choice((-1, 0, 1))})  # a timer
        lr._fit()
        pairs = {(law.x, law.y) for law in lr.laws}
        self.assertIn(("db|products|update", "http|inventory|/reserve"), pairs)
        k = next(law.k for law in lr.laws if {law.x, law.y} == {"db|products|update", "redis|set"})
        self.assertAlmostEqual(k if k > 1 else 1 / k, 2.0, places=1)
        self.assertFalse(any("supplier" in law.x or "supplier" in law.y for law in lr.laws),
                         "a steady poller must not become a law just because traffic was steady too")

    def _law(self):
        # calibrated like a real structural law: within about 3% on almost every healthy window
        return Law(X, Y, 1.0, [0.03 * i / 200 for i in range(1, 201)])

    def test_a_loss_breaks_the_law_only_after_confirmation(self):
        lr = quiet_learner()
        law = self._law()
        lr.laws = [law]
        for i in range(CONFIRM):
            lr._evaluate({X: 150, Y: 110}, {})
            self.assertEqual(law.status, "broken" if i == CONFIRM - 1 else "holds")

    def test_a_filling_queue_is_lag_not_loss(self):
        lr = quiet_learner()
        law = self._law()
        lr.laws = [law]
        for _ in range(CONFIRM + 1):
            lr._evaluate({X: 150, Y: 90}, {"q:work": 60})
        self.assertEqual(law.status, "lagging")

    def test_a_busy_queue_does_not_excuse_an_unrelated_small_loss(self):
        lr = quiet_learner()
        law = self._law()
        lr.laws = [law]
        for _ in range(CONFIRM):
            lr._evaluate({X: 150, Y: 125}, {"q:work": -400})
        self.assertEqual(law.status, "broken")

    def test_a_consumer_that_stops_while_work_piles_up_is_a_stall(self):
        lr = quiet_learner()
        law = self._law()
        lr.laws = [law]
        for _ in range(2 * CONFIRM):
            lr._evaluate({X: 150, Y: 0}, {"q:work": 150})
        self.assertEqual(law.status, "stalled")

    def test_an_isolated_miss_raises_the_bar_aci(self):
        lr = quiet_learner()
        law = self._law()
        lr.laws = [law]
        before = law.threshold()
        lr._evaluate({X: 150, Y: 110}, {})   # one miss
        lr._evaluate({X: 150, Y: 150}, {})   # then fine
        self.assertGreater(law.threshold(), before)

    def test_a_tight_law_needs_a_real_effect_to_break(self):
        # learned within 1%, the law must still not break over 7%: the flash-sale false alarm
        lr = quiet_learner()
        law = Law(X, Y, 1.0, [0.01 * i / 200 for i in range(1, 201)])
        lr.laws = [law]
        for _ in range(2 * CONFIRM):
            lr._evaluate({X: 150, Y: 140}, {})
        self.assertEqual(law.status, "holds")

    def test_no_regression_names_only_laws_that_broke_after_the_fix(self):
        lr = quiet_learner()
        old, held = self._law(), Law("http|orders|/orders", "db|orders|insert", 1.0, [0.03 * i / 200 for i in range(1, 201)])
        lr.laws = [old, held]
        for _ in range(CONFIRM):
            lr._evaluate({X: 150, Y: 0, held.x: 150, held.y: 150}, {})   # the fault broke one law
        baseline = lr.holding()
        self.assertEqual(baseline, {(held.x, held.y)})
        import time as _t
        fixed_at = _t.time()
        for _ in range(CONFIRM):
            lr._evaluate({X: 150, Y: 0, held.x: 150, held.y: 40}, {})    # the fix broke the other
        self.assertEqual([law.y for law in lr.regressions(baseline, after=fixed_at, nodes={"orders"})], [held.y])
        self.assertEqual(lr.regressions(baseline, after=fixed_at, nodes={"catalog-sync"}), [],
                         "a law that does not count the changed service's requests is not held against its fix")
        self.assertEqual(lr.regressions(baseline, after=_t.time() + 1, nodes={"orders"}), [],
                         "a law that started failing while the window still held the fault is not a regression")

    def test_a_repair_burst_pages_nobody(self):
        lr = quiet_learner()
        lr.quiet_until = 0.0
        law = self._law()
        lr.laws = [law]
        for _ in range(CONFIRM):
            lr._evaluate({X: 150, Y: 400}, {})  # Nabd just rebuilt hundreds of orders
        self.assertEqual(law.status, "broken")
        lr.hush()
        self.assertEqual(lr.alerts(), [], "the burst is Nabd's own repair, not a new fault")
        self.assertEqual(lr.node_health(), {})

    def test_a_changed_neighbour_of_the_stalled_flow_is_the_suspect(self):
        # the busiest pair breaks first (payments vs stock updates); inventory's own law has not broken yet,
        # but inventory moves with the stalled flow and was changed seconds before
        lr = quiet_learner()
        busy, own = Law("http|psp|/v1/charges", Y, 1.0, [0.01]), Law(X, Y, 1.0, [0.01])
        busy.status, busy.e, busy.since = "broken", -1.0, 1000.0
        lr.laws = [busy, own, Law("http|psp|/v1/charges", "http|auth|/verify", 1.0, [0.01])]
        saved = learned.config_store.history
        learned.config_store.history = lambda service, limit: [
            {"id": "dep-9", "service": "inventory", "author": "ci-bot", "ts": 990.0, "diff": {"reserve_mode": ["sync", "deferred"]}}]
        try:
            top = lr.localize(read_logs=False)[0]
        finally:
            learned.config_store.history = saved
        self.assertEqual((top["service"], top["change"]), ("inventory", "dep-9"))

    def test_counter_reset_counts_from_zero(self):
        lr = quiet_learner()
        lr.snaps.append((0.0, {"a": 1000.0}, {}))
        lr.snaps.append((31.0, {"a": 40.0}, {}))  # the service restarted
        flows, _ = lr._deltas()
        self.assertEqual(flows["a"], 40.0)

    def test_window_uses_the_newest_old_enough_snapshot(self):
        lr = quiet_learner()
        for t in range(0, 300, 5):
            lr.snaps.append((float(t), {"a": float(t)}, {}))
        flows, _ = lr._deltas()
        self.assertEqual(flows["a"], 30.0, "a 30 s window, not everything since the oldest snapshot")


class GateTests(unittest.TestCase):
    def test_validity_gate_refuses_a_restart_for_a_bad_deploy(self):
        hyp = {"node": "orders", "category": "bad_config_deploy"}
        self.assertFalse(actions.check_validity(hyp, "restart_service", {"service": "orders"})[0])
        self.assertTrue(actions.check_validity(hyp, "rollback_config", {"service": "orders"})[0])
        self.assertFalse(actions.check_validity(hyp, "rollback_config", {"service": "payments"})[0])

    def test_safety_gate(self):
        self.assertIsNotNone(actions.validate("flush_redis_keys", {"prefix": "q:order-events"}))
        self.assertIsNotNone(actions.validate("restart_service", {"service": "psp"}))
        self.assertIsNotNone(actions.validate("restart_service", {"service": "postgres"}))
        self.assertIsNotNone(actions.validate("terminate_db_sessions", {"application_name": "orders"}))
        self.assertIsNone(actions.validate("terminate_db_sessions", {"application_name": "nightly-report-job"}))

    def test_repairs_only_on_their_own_component(self):
        self.assertIsNotNone(actions.validate("replay_lost_events", {"service": "orders"}))
        self.assertEqual(actions.canonical_fix({"node": "fulfillment", "category": "poison_message"}),
                         {"name": "quarantine_stuck_message", "params": {"service": "fulfillment"}})


class RuleTests(unittest.TestCase):
    def _ev(self, **extra):
        ev = {"alerts": [], "firing": [], "logs": {}, "services": {}, "health": {}, "deploys": [], "probes": {},
              "containers": {}, "forecasts": {}, "redis": {}, "postgres": {}, "link": {}}
        ev.update(extra)
        return ev

    def test_learned_rule_rolls_back_the_changed_suspect(self):
        lr = {"state": "watching", "laws": 120, "flows": 20, "broken": 3, "lagging": 0, "first_break": 1000.0,
              "worst": [{"law": "1.00 updates on table products for every inventory /reserve",
                         "now": "updates on table products is 100% short", "missing": 150}],
              "suspects": [{"service": "inventory", "score": 2.1, "change": "dep-1005",
                            "evidence": ["config change dep-1005 on inventory 0.3 min before the first broken law"]}]}
        ev = self._ev(learned=lr, deploys=[{"id": "dep-1005", "service": "inventory", "ts": 980.0, "version": 2,
                                           "author": "ci-bot", "message": "perf", "minutes_ago": 0.3,
                                           "diff": {"reserve_mode": ["sync", "deferred"]}}])
        hyps = core_engine._learned_rule(ev)
        fix = next(h for h in hyps if h["action"]["name"] != "none")
        self.assertEqual((fix["node"], fix["category"], fix["action"]["name"]),
                         ("inventory", "bad_config_deploy", "rollback_config"))
        self.assertTrue(any(h["category"] == "silent_data_loss" for h in hyps), "the lost work goes to a human")

    def test_learned_rule_without_a_change_does_not_act(self):
        lr = {"state": "watching", "laws": 120, "flows": 20, "broken": 3, "first_break": 1000.0,
              "worst": [{"law": "l", "now": "n", "missing": 10}],
              "suspects": [{"service": "orders", "score": 0.3, "change": None, "evidence": ["3 of 15 laws broke"]}]}
        hyps = core_engine._learned_rule(self._ev(learned=lr))
        self.assertTrue(all(h["action"]["name"] == "none" for h in hyps))

    def test_books_honor_promises_and_refund_failures(self):
        books = {"charged_and_promised_but_no_order": {"count": 4, "sar": 900.0, "sample": ["pay_a"]},
                 "charged_but_checkout_failed": {"count": 2, "sar": 300.0, "sample": ["pay_b"]},
                 "orders_lost_before_fulfillment": {"count": 0, "sample": []}}
        inv = {k: {"ok": True, "title": k, "detail": ""} for k in ("money", "fulfilled", "conserved", "progress", "prices")}
        ev = self._ev(mizan={"invariants": inv, "books": books, "classic_alerts_firing": 0}, traces=[])
        heal = {h["action"]["name"]: h for h in core_engine._mizan_rules(ev) if h.get("phase") == "heal"}
        self.assertIn("replay_orphan_payments", heal)
        self.assertIn("refund_failed_payments", heal)
        self.assertEqual(heal["refund_failed_payments"]["node"], "payments")

    def test_books_catch_wrong_amounts_and_phantom_shipments(self):
        # both gaps were found by blind faults: every order existed, but with the wrong total;
        # and a made-up queue event was shipped although no order existed
        inv = {k: {"ok": True, "title": k, "detail": ""} for k in ("conserved", "progress", "prices")}
        inv["money"] = {"ok": False, "title": "money", "detail": "3 orders record a different amount",
                        "mismatched": 3, "mismatch_sample": ["ord-1"], "first_at": 1000.0}
        inv["fulfilled"] = {"ok": False, "title": "fulfilled", "detail": "1 shipment has no order",
                            "phantom_count": 1, "phantoms": ["ord-x"]}
        ev = self._ev(mizan={"invariants": inv, "books": {}, "classic_alerts_firing": 0}, traces=[])
        hyps = core_engine._mizan_rules(ev)
        found = {(h["node"], h["category"], h["action"]["name"]) for h in hyps}
        self.assertIn(("orders", "corrupted_data", "none"), found)
        self.assertIn(("events", "corrupted_data", "none"), found)
        for h in hyps:
            self.assertEqual(core_engine.skeptic(ev, h)["verdict"], "upheld")


class NoRegressionGuardTests(unittest.TestCase):
    """A fix that breaks a law which held before it is undone, and Nabd stops acting."""

    def _run(self, revert):
        import asyncio
        import time as _t
        from app import agent as agent_mod

        broken_law = Law("http|orders|/orders", "db|orders|insert", 1.0, [0.03])
        calls = []
        saved = (agent_mod.learner, agent_mod.actions.execute, agent_mod.asyncio.sleep)

        class FakeLearner:
            def regressions(self, held, after, nodes):
                return [broken_law] if (broken_law.x, broken_law.y) in held else []

        async def no_wait(_s):
            return None

        agent_mod.learner = FakeLearner()
        agent_mod.actions.execute = lambda name, params: calls.append((name, params)) or {"ok": True}
        agent_mod.asyncio.sleep = no_wait
        try:
            ag = agent_mod.Agent.__new__(agent_mod.Agent)
            inc = agent_mod.Incident(1, "reactive")
            ag.incident = inc
            events = []
            # executed 40 s ago: the window is already all "after" the change
            act = {"description": "Roll back orders", "status": "done", "executed_at": _t.time() - 40}
            asyncio.run(ag._no_regression(inc, act, {(broken_law.x, broken_law.y)}, "orders", revert,
                                          lambda t, **d: events.append((t, d))))
        finally:
            agent_mod.learner, agent_mod.actions.execute, agent_mod.asyncio.sleep = saved
        return inc, act, calls, events

    def test_a_fix_that_breaks_a_law_is_undone(self):
        undo = {"name": "restore_config", "params": {"service": "orders", "config": {"write_mode": "batched"}}}
        inc, act, calls, events = self._run(undo)
        self.assertEqual(calls, [("restore_config", undo["params"])])
        self.assertEqual(act["status"], "reverted")
        self.assertIn("reverted", inc.regression)
        self.assertEqual(events[0][0], "guard_reverted")

    def test_a_fix_that_cannot_be_undone_goes_to_a_human(self):
        inc, act, calls, events = self._run(None)
        self.assertEqual(calls, [])
        self.assertTrue(inc.regression)
        self.assertIn("guard_regression", [e[0] for e in events])
        self.assertTrue(inc.handoffs, "the person gets told what to check")


if __name__ == "__main__":
    unittest.main()
