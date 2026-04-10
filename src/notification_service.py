"""
Notification service — orchestrates the full message loop.

This is the façade that application code calls. It wires together the
order service and messaging service, enforces the communication rules,
and fires operations alerts automatically.

Message loop rules
------------------
1. Customer places an order → confirmation sent to customer, structured
   notification sent to business dashboard (not raw WhatsApp).
2. Customer amends a PENDING order → amendment logged, acknowledgement
   sent to customer.
3. Business advances order status → status notification sent to customer.
4. Operations alert fired automatically for:
   - High-value orders
   - Stock-out events
   - Fraud/anomaly events
5. Operations never receives the raw customer-business thread unless they
   explicitly pull the thread via the dashboard.
"""

from __future__ import annotations

from typing import List, Optional

from .models import (
    Business,
    Customer,
    NotificationTrigger,
    Order,
    OrderItem,
    OrderStatus,
)
from .messaging_service import MessagingService
from .order_service import HIGH_VALUE_THRESHOLD, OrderService


class NotificationService:
    """
    Façade that coordinates the full n:1:n communication loop.

    Usage::

        svc = NotificationService()
        order = svc.place_order(customer, business, items)
        svc.amend_order(order.order_id, "Remove the chia seeds")
        svc.advance_order(order.order_id, OrderStatus.PROCESSED)
    """

    def __init__(self,
                 order_service: Optional[OrderService] = None,
                 messaging_service: Optional[MessagingService] = None) -> None:
        self.orders = order_service or OrderService()
        self.messages = messaging_service or MessagingService()

    # ------------------------------------------------------------------
    # Customer actions
    # ------------------------------------------------------------------

    def place_order(self, customer: Customer, business: Business,
                    items: List[OrderItem]) -> Order:
        """
        Customer places a new order.

        1. Order created and persisted.
        2. Confirmation sent to customer.
        3. Structured notification logged for the business dashboard.
        4. High-value alert sent to operations if applicable.
        """
        order = self.orders.create_order(customer, business, items)
        self.messages.notify_order_received(order)
        self.messages.notify_business_new_order(order)

        if self.orders.is_high_value(order):
            self.messages.alert_operations(
                order,
                reason=f"High-value order (KES {order.total:,.0f} ≥ "
                       f"KES {HIGH_VALUE_THRESHOLD:,.0f})",
            )
        return order

    def amend_order(self, order_id: str, description: str) -> Order:
        """
        Customer requests an amendment to a PENDING order.

        The amendment is appended to the immutable audit trail and an
        acknowledgement is sent back to the customer.
        """
        amendment = self.orders.amend_order(order_id, description)
        order = self.orders.get_order(order_id)
        self.messages.notify_amendment_received(order, amendment.description)
        return order

    # ------------------------------------------------------------------
    # Business actions
    # ------------------------------------------------------------------

    def advance_order(self, order_id: str,
                      new_status: OrderStatus) -> Order:
        """
        Business advances an order to the next status.

        A status-update notification is sent to the customer automatically.
        """
        order = self.orders.advance_order(order_id, new_status)
        self.messages.notify_order_status(order)
        return order

    # ------------------------------------------------------------------
    # Operations actions
    # ------------------------------------------------------------------

    def trigger_operations_alert(self, order_id: str,
                                  trigger: NotificationTrigger,
                                  detail: str = "") -> Order:
        """
        Manually fire an operations alert for a specific trigger.

        Operations normally only intervene when automatic thresholds are
        crossed, but this method allows explicit escalation.
        """
        order = self.orders._get_or_raise(order_id)
        reason = f"{trigger.value}: {detail}" if detail else trigger.value
        self.messages.alert_operations(order, reason=reason)
        return order
