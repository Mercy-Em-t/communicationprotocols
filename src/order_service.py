"""
Order service — manages order lifecycle and state transitions.

Valid transitions:
  PENDING → PROCESSED → DELIVERED
  PENDING → CANCELLED
  PROCESSED → CANCELLED
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import (
    Amendment,
    Business,
    Customer,
    Order,
    OrderItem,
    OrderStatus,
)

# High-value threshold (KES) that triggers an operations alert
HIGH_VALUE_THRESHOLD = 5_000.0

ALLOWED_TRANSITIONS: Dict[OrderStatus, List[OrderStatus]] = {
    OrderStatus.PENDING: [OrderStatus.PROCESSED, OrderStatus.CANCELLED],
    OrderStatus.PROCESSED: [OrderStatus.DELIVERED, OrderStatus.CANCELLED],
    OrderStatus.DELIVERED: [],
    OrderStatus.CANCELLED: [],
}


class OrderTransitionError(Exception):
    """Raised when an invalid order status transition is attempted."""


class OrderService:
    """
    Central service for creating and advancing orders.

    All orders are stored in memory in ``_orders`` and indexed
    by customer and business for efficient dashboard lookups.

    Args:
        high_value_threshold: Orders at or above this amount (KES) will
            trigger an operations alert. Defaults to ``HIGH_VALUE_THRESHOLD``.
    """

    def __init__(self,
                 high_value_threshold: float = HIGH_VALUE_THRESHOLD) -> None:
        self._orders: Dict[str, Order] = {}
        self.high_value_threshold = high_value_threshold

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_order(self, customer: Customer, business: Business,
                     items: List[OrderItem]) -> Order:
        """Create a new order and persist it."""
        order = Order.create(customer, business, items)
        self._orders[order.order_id] = order
        return order

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_orders_for_business(self, business_id: str) -> List[Order]:
        """Return all orders for a given business (dashboard use)."""
        return [o for o in self._orders.values()
                if o.business.business_id == business_id]

    def get_orders_for_customer(self, customer_id: str) -> List[Order]:
        return [o for o in self._orders.values()
                if o.customer.customer_id == customer_id]

    def get_all_orders(self) -> List[Order]:
        return list(self._orders.values())

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def advance_order(self, order_id: str,
                      new_status: OrderStatus) -> Order:
        """
        Move an order to ``new_status``.

        Raises ``OrderTransitionError`` if the transition is not allowed.
        """
        order = self._get_or_raise(order_id)
        if new_status not in ALLOWED_TRANSITIONS[order.status]:
            raise OrderTransitionError(
                f"Cannot move order {order_id} from "
                f"{order.status.value} to {new_status.value}."
            )
        order.status = new_status
        return order

    # ------------------------------------------------------------------
    # Amendments
    # ------------------------------------------------------------------

    def amend_order(self, order_id: str, description: str) -> Amendment:
        """
        Record an amendment on an order that is still PENDING.

        Amendments are immutable records appended to the order's audit trail.
        """
        order = self._get_or_raise(order_id)
        if order.status != OrderStatus.PENDING:
            raise OrderTransitionError(
                f"Order {order_id} cannot be amended in "
                f"{order.status.value} state."
            )
        return order.add_amendment(description)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_high_value(self, order: Order) -> bool:
        return order.total >= self.high_value_threshold

    def _get_or_raise(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Order {order_id!r} not found.")
        return order
