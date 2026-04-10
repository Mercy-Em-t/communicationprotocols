"""
Operations dashboard — aggregated, read-only view for the ops team.

Operations never see 500 raw WhatsApp threads. They see summary stats
per business and can drill into individual threads only when needed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Message, MessageThread, Order, OrderStatus
from .messaging_service import MessagingService
from .order_service import OrderService


@dataclass
class BusinessSummary:
    """Aggregated stats for a single business."""
    business_id: str
    business_name: str
    total_orders: int = 0
    pending: int = 0
    processed: int = 0
    delivered: int = 0
    cancelled: int = 0
    total_revenue: float = 0.0
    amendments: int = 0


@dataclass
class DashboardReport:
    """Full dashboard snapshot consumed by the operations team."""
    summaries: List[BusinessSummary] = field(default_factory=list)
    operations_alerts: List[Message] = field(default_factory=list)

    @property
    def total_orders(self) -> int:
        return sum(s.total_orders for s in self.summaries)

    @property
    def total_pending(self) -> int:
        return sum(s.pending for s in self.summaries)


class OperationsDashboard:
    """
    Read-only aggregated view for the operations team.

    Operations can:
    - See summary stats per business (order counts, revenue, amendments).
    - View their own alert queue (high-value, fraud, stock-out events).
    - Drill into a single order thread when intervention is necessary.

    Operations cannot directly push messages to customers via this class;
    that goes through NotificationService.trigger_operations_alert().
    """

    def __init__(self, order_service: OrderService,
                 messaging_service: MessagingService) -> None:
        self._orders = order_service
        self._messages = messaging_service

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def generate_report(self, *, tenant_id: Optional[str] = None,
                        business_id: Optional[str] = None,
                        status: Optional[OrderStatus] = None,
                        page: int = 1, page_size: int = 50) -> DashboardReport:
        """
        Build the full dashboard snapshot.

        Aggregates all orders by business and collects operations-bound
        alerts — giving ops a bird's-eye view without exposing every
        individual customer message.
        """
        by_business: Dict[str, BusinessSummary] = {}

        for order in self._orders.get_all_orders(tenant_id=tenant_id):
            if business_id is not None and order.business.business_id != business_id:
                continue
            if status is not None and order.status != status:
                continue
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

            if order.status == OrderStatus.PENDING:
                summary.pending += 1
            elif order.status == OrderStatus.PROCESSED:
                summary.processed += 1
            elif order.status == OrderStatus.DELIVERED:
                summary.delivered += 1
            elif order.status == OrderStatus.CANCELLED:
                summary.cancelled += 1

        all_summaries = list(by_business.values())
        start = max(0, (page - 1) * page_size)
        end = start + page_size
        alerts = self._collect_ops_alerts(tenant_id=tenant_id, page=page, page_size=page_size)
        return DashboardReport(
            summaries=all_summaries[start:end],
            operations_alerts=alerts,
        )

    # ------------------------------------------------------------------
    # Drill-down
    # ------------------------------------------------------------------

    def get_order_thread(self, order_id: str,
                         tenant_id: Optional[str] = None) -> Optional[MessageThread]:
        """
        Retrieve the full message thread for a specific order.

        Used only when operations need to investigate a flagged order.
        """
        return self._messages.get_thread(order_id, tenant_id=tenant_id)

    def get_business_orders(self, business_id: str,
                            tenant_id: Optional[str] = None) -> List[Order]:
        """Return all orders for a specific business."""
        return self._orders.get_orders_for_business(business_id, tenant_id=tenant_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_ops_alerts(self, tenant_id: Optional[str] = None,
                            page: int = 1, page_size: int = 50) -> List[Message]:
        """Gather all messages addressed to operations across all threads."""
        alerts: List[Message] = []
        for order in self._orders.get_all_orders(tenant_id=tenant_id):
            thread = self._messages.get_thread(order.order_id, tenant_id=tenant_id)
            if thread is None:
                continue
            for msg in thread.messages:
                if msg.recipient == "operations":
                    alerts.append(msg)
        start = max(0, (page - 1) * page_size)
        end = start + page_size
        return alerts[start:end]
