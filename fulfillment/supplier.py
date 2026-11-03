"""공급처 API 경계.

공급처 응답을 세 가지로만 나눈다.
- 성공: 구매가 확정됐다.
- Rejected: 공급처가 명확히 거절했다. 돈이 나가지 않았으니 다시 시도해도 된다.
- Ambiguous: 5xx·타임아웃·연결 끊김. 결제가 됐는지 알 수 없다. **다시 사면 안 된다.**
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SupplierError(Exception):
    pass


class Rejected(SupplierError):
    """공급처가 명확히 거절함 (재고 없음, 잘못된 요청 등)."""


class Ambiguous(SupplierError):
    """결과를 알 수 없음. 구매가 됐을 수도, 안 됐을 수도 있다."""


@dataclass(frozen=True)
class Purchase:
    supplier_order_id: str
    sku: str
    quantity: int
    created_at: float  # 공급처 기록상 주문 시각 (epoch seconds)


class Supplier(Protocol):
    def buy(self, sku: str, quantity: int, idempotency_key: str) -> Purchase: ...

    def history(self, since: float) -> list[Purchase]:
        """since 이후 공급처에 남은 구매 이력. 결과를 모를 때 대조용."""
        ...
