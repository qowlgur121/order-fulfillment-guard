"""운영 중 실제로 신경 썼던 상황을 재현 테스트로 고정한다."""
import threading

import pytest

from fulfillment import store, worker
from fulfillment.supplier import Ambiguous, Purchase, Rejected

T0 = 1_700_000_000.0


class FakeSupplier:
    """공급처 흉내. 응답 순서를 정해 두고, 실제로 산 건 history에 남긴다."""

    def __init__(self, *responses):
        self.responses = list(responses)  # "ok" / "reject" / "ambiguous_paid" / "ambiguous_unpaid"
        self.bought: list[Purchase] = []
        self.buy_calls = 0
        self.history_fails = False
        self.extra_history: list[Purchase] = []

    def buy(self, sku, quantity, idempotency_key):
        self.buy_calls += 1
        r = self.responses.pop(0) if self.responses else "ok"
        if r == "reject":
            raise Rejected("out of stock")
        p = Purchase(f"S-{len(self.bought) + 1}", sku, quantity, self.clock)
        if r in ("ok", "ambiguous_paid"):
            self.bought.append(p)
        if r.startswith("ambiguous"):
            raise Ambiguous("502 Bad Gateway")
        return p

    def history(self, since):
        if self.history_fails:
            raise ConnectionError("history down")
        return [p for p in self.bought + self.extra_history if p.created_at >= since]

    clock = T0


@pytest.fixture
def conn():
    c = store.connect()
    store.add_order(c, "A1", "SKU-1", 1)
    return c


def test_정상_구매(conn):
    sup = FakeSupplier("ok")
    assert worker.process(conn, sup, "A1", now=T0) == "BOUGHT"
    assert store.get_attempt(conn, "A1")["status"] == "CONFIRMED"


def test_같은_주문번호는_한_번만_들어간다(conn):
    assert store.add_order(conn, "A1", "SKU-1", 1) is False
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_결과를_모르면_다시_사지_않고_이력에서_복구한다(conn):
    sup = FakeSupplier("ambiguous_paid")
    assert worker.process(conn, sup, "A1", now=T0) == "BOUGHT"
    assert sup.buy_calls == 1                      # 재구매 없음
    assert store.get_attempt(conn, "A1")["supplier_order_id"] == "S-1"


def test_결과를_모르고_이력에도_없으면_기다리며_재구매하지_않는다(conn):
    sup = FakeSupplier("ambiguous_unpaid")
    assert worker.process(conn, sup, "A1", now=T0) == "RETRY_WAIT"
    # 재시도 시각이 지나 다시 돌아도 구매 API는 부르지 않고 대조만 한다
    nxt = store.get_order(conn, "A1")["next_retry_at"]
    assert worker.process(conn, sup, "A1", now=nxt) == "RETRY_WAIT"
    assert sup.buy_calls == 1


def test_공급처_반영이_늦어도_다음_대조에서_복구한다(conn):
    sup = FakeSupplier("ambiguous_unpaid")
    worker.process(conn, sup, "A1", now=T0)
    sup.extra_history.append(Purchase("S-late", "SKU-1", 1, T0 + 2))  # 뒤늦게 이력에 나타남
    nxt = store.get_order(conn, "A1")["next_retry_at"]
    assert worker.process(conn, sup, "A1", now=nxt) == "BOUGHT"
    assert sup.buy_calls == 1


def test_유예_시간이_지나도_0건이면_사람_검토(conn):
    sup = FakeSupplier("ambiguous_unpaid")
    alerts = worker.Alerts()
    worker.process(conn, sup, "A1", now=T0, alert=alerts)
    later = T0 + worker.RECONCILE_GRACE + 1
    assert worker.process(conn, sup, "A1", now=later, alert=alerts) == "NEEDS_REVIEW"
    assert alerts.items and alerts.items[-1][0] == "A1"
    assert sup.buy_calls == 1


def test_이력_후보가_여러_건이면_사람_검토(conn):
    sup = FakeSupplier("ambiguous_paid")
    sup.extra_history.append(Purchase("S-other", "SKU-1", 1, T0 + 1))  # 같은 상품·수량의 다른 구매
    alerts = worker.Alerts()
    assert worker.process(conn, sup, "A1", now=T0, alert=alerts) == "NEEDS_REVIEW"
    assert sup.buy_calls == 1


def test_이력_조회가_실패해도_추측하지_않는다(conn):
    sup = FakeSupplier("ambiguous_unpaid")
    sup.history_fails = True
    assert worker.process(conn, sup, "A1", now=T0) == "RETRY_WAIT"
    assert store.get_attempt(conn, "A1")["status"] == "AMBIGUOUS"


def test_요청_중_프로세스가_죽어_POSTING이_남으면_결과_모름으로_본다(conn):
    store.save_attempt(conn, "A1", "POSTING", started_at=T0)  # 공급처 호출 직전 기록만 남고 죽은 상황
    sup = FakeSupplier()
    sup.extra_history.append(Purchase("S-9", "SKU-1", 1, T0 + 1))
    assert worker.process(conn, sup, "A1", now=T0 + 60) == "BOUGHT"
    assert sup.buy_calls == 0


def test_확정_직후_죽었으면_다시_사지_않는다(conn):
    store.save_attempt(conn, "A1", "CONFIRMED", started_at=T0, supplier_order_id="S-1")
    sup = FakeSupplier()
    assert worker.process(conn, sup, "A1", now=T0 + 60) == "BOUGHT"
    assert sup.buy_calls == 0


def test_명확한_거절은_백오프_후_재시도한다(conn):
    sup = FakeSupplier("reject", "ok")
    assert worker.process(conn, sup, "A1", now=T0) == "RETRY_WAIT"
    nxt = store.get_order(conn, "A1")["next_retry_at"]
    assert nxt == T0 + 30
    assert worker.process(conn, sup, "A1", now=nxt - 1) == "RETRY_WAIT"  # 아직 차례 아님
    assert worker.process(conn, sup, "A1", now=nxt) == "BOUGHT"


@pytest.mark.parametrize("attempts,expected", [(1, 30), (2, 60), (3, 120), (10, 900)])
def test_백오프는_30초에서_15분까지(attempts, expected):
    assert worker.backoff(attempts) == expected


def test_비상_정지(conn):
    store.set_setting(conn, "kill_switch", "on")
    sup = FakeSupplier("ok")
    alerts = worker.Alerts()
    assert worker.process(conn, sup, "A1", now=T0, alert=alerts) == "RETRY_WAIT"
    assert sup.buy_calls == 0 and alerts.items


def test_하루_구매_상한(conn):
    store.set_setting(conn, "daily_cap", "1")
    store.add_order(conn, "A2", "SKU-1", 1)
    sup = FakeSupplier("ok", "ok")
    assert worker.process(conn, sup, "A1", now=T0, day_start=T0 - 10) == "BOUGHT"
    assert worker.process(conn, sup, "A2", now=T0 + 1, day_start=T0 - 10) == "RETRY_WAIT"
    assert sup.buy_calls == 1


def test_워커_둘이_같은_주문을_동시에_잡아도_한_명만_잡는다(tmp_path):
    path = str(tmp_path / "db.sqlite")
    store.add_order(store.connect(path), "A1", "SKU-1", 1)
    results, barrier = [], threading.Barrier(2)

    def run():
        c = store.connect(path)
        c.execute("PRAGMA busy_timeout=2000")
        barrier.wait()
        results.append(store.claim(c, "A1", T0))

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [False, True]
