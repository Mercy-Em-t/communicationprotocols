"""
Operations dashboard — aggregated, read-only lifecycle view for operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .messaging_service import MessagingService
from .models import Message, MessageThread, Order, OrderStatus
from .order_service import OrderService


@dataclass
class BusinessSummary:
    """Aggregated lifecycle stats for a single business."""
    business_id: str
    business_name: str
    total_orders: int = 0
    total_revenue: float = 0.0
    amendments: int = 0
    status_counts: Dict[str, int] = field(default_factory=dict)
    rejected_orders: int = 0
    paid_orders: int = 0
    completed_orders: int = 0
    cancelled_orders: int = 0
    payment_completion_rate: float = 0.0
    rejection_rate: float = 0.0
    avg_fulfillment_lead_time_minutes: float = 0.0
    stuck_orders: int = 0


@dataclass
class DashboardReport:
    """Full dashboard snapshot consumed by operations."""
    summaries: List[BusinessSummary] = field(default_factory=list)
    operations_alerts: List[Message] = field(default_factory=list)
    conversion_by_state: Dict[str, float] = field(default_factory=dict)
    sla_breached_orders: List[str] = field(default_factory=list)

    @property
    def total_orders(self) -> int:
        return sum(s.total_orders for s in self.summaries)


class OperationsDashboard:
    """Read-only aggregated view for the operations team."""

    def __init__(self, order_service: OrderService,
                 messaging_service: MessagingService) -> None:
        self._orders = order_service
        self._messages = messaging_service

    def generate_report(self, *, tenant_id: Optional[str] = None,
                        business_id: Optional[str] = None,
                        status: Optional[OrderStatus] = None,
                        page: int = 1, page_size: int = 50) -> DashboardReport:
        by_business: Dict[str, BusinessSummary] = {}

        filtered_orders: List[Order] = []
        for order in self._orders.get_all_orders(tenant_id=tenant_id):
            if business_id is not None and order.business.business_id != business_id:
                continue
            if status is not None and order.status != status:
                continue
            filtered_orders.append(order)

            bid = order.business.business_id
            if bid not in by_business:
                by_business[bid] = BusinessSummary(
                    business_id=bid,
                    business_name=order.business.name,
                )
            summary = by_business[bid]
            summary.total_orders += 1
            summary.total_revenue += order.total
            summary.amendments += len(order.amendments)

            status_key = order.status.value
            summary.status_counts[status_key] = summary.status_counts.get(status_key, 0) + 1

            if order.status == OrderStatus.REJECTED_BY_SHOP:
                summary.rejected_orders += 1
            if order.status in {
                OrderStatus.PAID,
                OrderStatus.FULFILLING,
                OrderStatus.READY_FOR_PICKUP,
                OrderStatus.OUT_FOR_DELIVERY,
                OrderStatus.COMPLETED,
            }:
                summary.paid_orders += 1
            if order.status == OrderStatus.COMPLETED:
                summary.completed_orders += 1
            if order.status == OrderStatus.CANCELLED:
                summary.cancelled_orders += 1
            if order.status in {
                OrderStatus.PENDING_SHOP_CONFIRMATION,
                OrderStatus.AWAITING_CUSTOMER_CONFIRMATION,
                OrderStatus.AWAITING_PAYMENT,
                OrderStatus.PAYMENT_PROCESSING,
            }:
                summary.stuck_orders += 1

        for summary in by_business.values():
            if summary.total_orders > 0:
                summary.rejection_rate = summary.rejected_orders / summary.total_orders
                summary.payment_completion_rate = summary.paid_orders / summary.total_orders

            business_orders = [
                o for o in filtered_orders
                if o.business.business_id == summary.business_id and o.status == OrderStatus.COMPLETED
            ]
            if business_orders:
                total_minutes = sum(
                    max(0.0, (o.updated_at - o.created_at).total_seconds() / 60.0)
                    for o in business_orders
                )
                summary.avg_fulfillment_lead_time_minutes = total_minutes / len(business_orders)

        all_summaries = list(by_business.values())
        start = max(0, (page - 1) * page_size)
        end = start + page_size
        alerts = self._collect_ops_alerts(tenant_id=tenant_id)

        conversion_by_state: Dict[str, float] = {}
        total = len(filtered_orders)
        if total > 0:
            counts: Dict[str, int] = {}
            for order in filtered_orders:
                k = order.status.value
                counts[k] = counts.get(k, 0) + 1
            conversion_by_state = {k: v / total for k, v in counts.items()}

        sla_breached_orders = self._extract_sla_breaches(alerts)

        return DashboardReport(
            summaries=all_summaries[start:end],
            operations_alerts=alerts,
            conversion_by_state=conversion_by_state,
            sla_breached_orders=sla_breached_orders,
        )

    def get_order_thread(self, order_id: str,
                         tenant_id: Optional[str] = None) -> Optional[MessageThread]:
        return self._messages.get_thread(order_id, tenant_id=tenant_id)

    def get_business_orders(self, business_id: str,
                            tenant_id: Optional[str] = None) -> List[Order]:
        return self._orders.get_orders_for_business(business_id, tenant_id=tenant_id)

    def _collect_ops_alerts(self, tenant_id: Optional[str] = None) -> List[Message]:
        alerts: List[Message] = []
        for order in self._orders.get_all_orders(tenant_id=tenant_id):
            thread = self._messages.get_thread(order.order_id, tenant_id=tenant_id)
            if thread is None:
                continue
            for msg in thread.messages:
                if msg.recipient == "operations":
                    alerts.append(msg)
        return alerts

    @staticmethod
    def _extract_sla_breaches(alerts: List[Message]) -> List[str]:
        out: List[str] = []
        for msg in alerts:
            if "sla_breach" in msg.body.lower() and msg.order_id not in out:
                out.append(msg.order_id)
        return out
