"""주문 한 건을 처리하는 흐름.

    주문 잡기(claim) → 안전장치 확인 → 구매 시도 기록(POSTING) → 공급처 구매
      ├─ 성공      → CONFIRMED, 주문 BOUGHT
      ├─ 거절      → REJECTED, 재시도 대기(지수 백오프)
      └─ 결과 모름 → AMBIGUOUS, 재구매 금지, 공급처 이력과 대조
                       ├─ 정확히 1건 일치 → CONFIRMED로 복구
                       ├─ 0건 → 잠시 뒤 다시 대조 (공급처 반영이 늦을 수 있음)
                       │        유예 시간이 지나도 0건이면 사람 검토
                       └─ 여러 건 → 어느 것이 이 주문인지 모름 → 사람 검토

핵심 판단: 결과를 모르는 주문은 처리가 몇 분 늦어지는 것을 감수한다.
늦는 것보다 두 번 사는 쪽이 훨씬 비싸다.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from . import store
from .supplier import Ambiguous, Purchase, Rejected, Supplier

BACKOFF_MIN = 30.0          # 첫 재시도 30초
BACKOFF_MAX = 15 * 60.0     # 최대 15분
RECONCILE_GRACE = 15 * 60.0  # 결과 모름 상태에서 0건이 이만큼 이어지면 사람 검토로 넘긴다
MATCH_WINDOW = 5 * 60.0     # 구매 시도 시각부터 이 시간 안의 이력만 같은 주문 후보로 본다


def backoff(attempts: int) -> float:
    """30초, 60초, 120초 … 최대 15분."""
    return min(BACKOFF_MAX, BACKOFF_MIN * (2 ** max(0, attempts - 1)))


@dataclass
class Alerts:
    """사람에게 알릴 일. 실제 운영에서는 메신저 알림으로 보낸다."""
    items: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, order_id: str, message: str) -> None:
        self.items.append((order_id, message))


def _retry_later(conn: sqlite3.Connection, order_id: str, now: float) -> None:
    order = store.get_order(conn, order_id)
    attempts = order["attempts"] + 1
    store.transition(conn, order_id, "PROCESSING", "RETRY_WAIT",
                     attempts=attempts, next_retry_at=now + backoff(attempts))


def _needs_review(conn: sqlite3.Connection, order_id: str, alert: Callable[[str, str], None], why: str) -> None:
    store.transition(conn, order_id, "PROCESSING", "NEEDS_REVIEW")
    alert(order_id, why)


def _match_history(supplier: Supplier, order: sqlite3.Row, started_at: float) -> list[Purchase]:
    return [
        p for p in supplier.history(since=started_at)
        if p.sku == order["sku"] and p.quantity == order["quantity"]
        and started_at <= p.created_at <= started_at + MATCH_WINDOW
    ]


def _reconcile(conn: sqlite3.Connection, supplier: Supplier, order: sqlite3.Row, attempt: sqlite3.Row,
               now: float, alert: Callable[[str, str], None]) -> str:
    """결과를 모르는 구매를 공급처 이력과 맞춰 본다. 반환값은 주문의 다음 상태."""
    order_id = order["order_id"]
    try:
        matches = _match_history(supplier, order, attempt["started_at"])
    except Exception:
        # 이력 조회마저 실패하면 판단하지 않고 나중에 다시 본다.
        _retry_later(conn, order_id, now)
        return "RETRY_WAIT"

    if len(matches) == 1:
        store.save_attempt(conn, order_id, "CONFIRMED", supplier_order_id=matches[0].supplier_order_id)
        store.transition(conn, order_id, "PROCESSING", "BOUGHT")
        return "BOUGHT"
    if len(matches) > 1:
        _needs_review(conn, order_id, alert, f"공급처 이력에 후보 {len(matches)}건 — 어느 것이 이 주문인지 판단 불가")
        return "NEEDS_REVIEW"
    if now - attempt["started_at"] < RECONCILE_GRACE:
        _retry_later(conn, order_id, now)  # 공급처 반영이 늦을 수 있으니 다시 대조한다. 재구매는 하지 않는다.
        return "RETRY_WAIT"
    _needs_review(conn, order_id, alert, "결과를 모르는 구매가 유예 시간 동안 이력에 나타나지 않음 — 결제 여부 확인 필요")
    return "NEEDS_REVIEW"


def _guard(conn: sqlite3.Connection, order_id: str, now: float, day_start: float,
           alert: Callable[[str, str], None]) -> bool:
    """돈이 나가기 전 마지막 안전장치. 막히면 주문은 기다리고 사람에게 알린다."""
    if store.get_setting(conn, "kill_switch", "off") == "on":
        _retry_later(conn, order_id, now)
        alert(order_id, "비상 정지가 켜져 있어 구매를 멈춤")
        return False
    cap = int(store.get_setting(conn, "daily_cap", "100"))
    if store.confirmed_purchases_since(conn, day_start) >= cap:
        _retry_later(conn, order_id, now)
        alert(order_id, f"하루 구매 상한 {cap}건에 도달 — 버그로 인한 연속 구매를 막기 위해 멈춤")
        return False
    return True


def process(conn: sqlite3.Connection, supplier: Supplier, order_id: str, *,
            now: float | None = None, day_start: float = 0.0,
            alert: Callable[[str, str], None] = lambda *_: None) -> str:
    """주문 한 건을 한 단계 진행시키고 주문의 상태를 돌려준다."""
    now = time.time() if now is None else now
    if not store.claim(conn, order_id, now):
        order = store.get_order(conn, order_id)
        return order["status"] if order else "UNKNOWN"

    order = store.get_order(conn, order_id)
    attempt = store.get_attempt(conn, order_id)

    # 이미 확정된 구매가 있으면 다시 사지 않는다 (예: 확정 직후 프로세스가 죽은 경우).
    if attempt is not None and attempt["status"] == "CONFIRMED":
        store.transition(conn, order_id, "PROCESSING", "BOUGHT")
        return "BOUGHT"
    # 결과를 모르는 구매가 남아 있으면 재구매하지 않고 대조만 한다.
    # POSTING이 남아 있다는 것은 요청 중에 프로세스가 죽었다는 뜻이므로 똑같이 취급한다.
    if attempt is not None and attempt["status"] in ("AMBIGUOUS", "POSTING"):
        store.save_attempt(conn, order_id, "AMBIGUOUS")
        return _reconcile(conn, supplier, order, store.get_attempt(conn, order_id), now, alert)

    if not _guard(conn, order_id, now, day_start, alert):
        return "RETRY_WAIT"

    # 공급처를 부르기 전에 "지금 산다"를 먼저 남긴다. 호출 중 죽어도 흔적이 남는다.
    store.save_attempt(conn, order_id, "POSTING", started_at=now)
    try:
        purchase = supplier.buy(order["sku"], order["quantity"], idempotency_key=f"order-{order_id}")
    except Rejected:
        store.save_attempt(conn, order_id, "REJECTED")
        _retry_later(conn, order_id, now)
        return "RETRY_WAIT"
    except Ambiguous:
        store.save_attempt(conn, order_id, "AMBIGUOUS")
        return _reconcile(conn, supplier, order, store.get_attempt(conn, order_id), now, alert)

    store.save_attempt(conn, order_id, "CONFIRMED", supplier_order_id=purchase.supplier_order_id)
    store.transition(conn, order_id, "PROCESSING", "BOUGHT")
    return "BOUGHT"
