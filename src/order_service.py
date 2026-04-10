"""
Order service — manages order lifecycle and state transitions.

Valid transitions:
  CREATED → PENDING_SHOP_CONFIRMATION
  PENDING_SHOP_CONFIRMATION → ACCEPTED_BY_SHOP | REJECTED_BY_SHOP
  ACCEPTED_BY_SHOP → AWAITING_CUSTOMER_CONFIRMATION → AWAITING_PAYMENT
  AWAITING_PAYMENT → PAYMENT_PROCESSING → PAID
  PAID → FULFILLING → READY_FOR_PICKUP | OUT_FOR_DELIVERY → COMPLETED
  CANCELLED can occur in mutable states
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .auth import AuthContext, Role, authorize_order_action, authorize_order_read
from .inventory_service import InventoryError, InventoryService
from .models import (
    Amendment,
    Business,
    Customer,
    Order,
    OrderItem,
    OrderStatus,
)
from .persistence import JsonPersistence, deserialize_orders, serialize_order

# High-value threshold in KES (Kenyan Shillings) that triggers an operations alert
HIGH_VALUE_THRESHOLD = 5_000.0

ALLOWED_TRANSITIONS: Dict[OrderStatus, List[OrderStatus]] = {
    OrderStatus.CREATED: [OrderStatus.PENDING_SHOP_CONFIRMATION, OrderStatus.CANCELLED],
    OrderStatus.PENDING_SHOP_CONFIRMATION: [
        OrderStatus.ACCEPTED_BY_SHOP,
        OrderStatus.REJECTED_BY_SHOP,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.REJECTED_BY_SHOP: [],
    OrderStatus.ACCEPTED_BY_SHOP: [
        OrderStatus.AWAITING_CUSTOMER_CONFIRMATION,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.AWAITING_CUSTOMER_CONFIRMATION: [
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.AWAITING_PAYMENT: [
        OrderStatus.PAYMENT_PROCESSING,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.PAYMENT_PROCESSING: [
        OrderStatus.PAID,
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.PAID: [OrderStatus.FULFILLING, OrderStatus.CANCELLED],
    OrderStatus.FULFILLING: [
        OrderStatus.READY_FOR_PICKUP,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.READY_FOR_PICKUP: [OrderStatus.COMPLETED, OrderStatus.CANCELLED],
    OrderStatus.OUT_FOR_DELIVERY: [OrderStatus.COMPLETED, OrderStatus.CANCELLED],
    OrderStatus.COMPLETED: [],
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
                 high_value_threshold: float = HIGH_VALUE_THRESHOLD,
                 persistence: Optional[JsonPersistence] = None,
                 inventory_service: Optional[InventoryService] = None) -> None:
        self._orders: Dict[str, Order] = {}
        self._idempotency_index: Dict[str, str] = {}
        self.persistence = persistence or JsonPersistence()
        self.inventory = inventory_service or InventoryService()
        self.high_value_threshold = high_value_threshold
        self._load()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_order(self, customer: Customer, business: Business,
                     items: List[OrderItem], *,
                     idempotency_key: Optional[str] = None,
                     actor: Optional[AuthContext] = None) -> Order:
        """Create a new order and persist it (initial state: CREATED)."""
        if actor is not None and actor.role != Role.SYSTEM:
            if actor.role != Role.CUSTOMER or actor.actor_id != customer.customer_id:
                raise PermissionError("Only the customer can create their order.")
            if actor.tenant_id != customer.tenant_id:
                raise PermissionError("Cross-tenant create is not allowed.")
        if idempotency_key and idempotency_key in self._idempotency_index:
            existing_id = self._idempotency_index[idempotency_key]
            return self._orders[existing_id]
        order = Order.create(customer, business, items)
        self.inventory.reserve_for_order(order)
        self._orders[order.order_id] = order
        if idempotency_key:
            self._idempotency_index[idempotency_key] = order.order_id
        self._save()
        return order

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_order(self, order_id: str,
                  actor: Optional[AuthContext] = None) -> Optional[Order]:
        order = self._orders.get(order_id)
        if order is None:
            return None
        authorize_order_read(actor, order)
        return order

    def get_orders_for_business(self, business_id: str,
                                tenant_id: Optional[str] = None) -> List[Order]:
        """Return all orders for a given business (dashboard use)."""
        return [o for o in self._orders.values()
                if o.business.business_id == business_id
                and (tenant_id is None or o.tenant_id == tenant_id)]

    def get_orders_for_customer(self, customer_id: str,
                                tenant_id: Optional[str] = None) -> List[Order]:
        return [o for o in self._orders.values()
                if o.customer.customer_id == customer_id
                and (tenant_id is None or o.tenant_id == tenant_id)]

    def get_all_orders(self, tenant_id: Optional[str] = None) -> List[Order]:
        if tenant_id is None:
            return list(self._orders.values())
        return [o for o in self._orders.values() if o.tenant_id == tenant_id]

    def get_order_or_raise(self, order_id: str) -> Order:
        return self._get_or_raise(order_id)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def advance_order(self, order_id: str,
                      new_status: OrderStatus,
                      actor: Optional[AuthContext] = None) -> Order:
        """
        Move an order to ``new_status``.

        Raises ``OrderTransitionError`` if the transition is not allowed.
        """
        order = self._get_or_raise(order_id)
        authorize_order_action(actor, order, (Role.BUSINESS, Role.OPERATIONS, Role.SYSTEM))
        if new_status not in ALLOWED_TRANSITIONS[order.status]:
            raise OrderTransitionError(
                f"Cannot move order {order_id} from "
                f"{order.status.value} to {new_status.value}."
            )
        order.status = new_status
        order.updated_at = datetime.now(tz=timezone.utc)
        if new_status in {OrderStatus.CANCELLED, OrderStatus.REJECTED_BY_SHOP}:
            self.inventory.release_for_order(order.order_id)
        self._save()
        return order

    # ------------------------------------------------------------------
    # Amendments
    # ------------------------------------------------------------------

    def amend_order(self, order_id: str, description: str,
                    actor: Optional[AuthContext] = None) -> Amendment:
        """
        Record an amendment while the order is still modifiable.

        Amendments are immutable records appended to the order's audit trail.
        """
        order = self._orders.get(order_id)
        if order is None:
            if actor is not None:
                raise PermissionError("Not allowed to amend this order.")
            raise KeyError(f"Order {order_id!r} not found.")
        authorize_order_action(
            actor, order, (Role.CUSTOMER, Role.BUSINESS, Role.OPERATIONS, Role.SYSTEM)
        )
        if order.status not in {
            OrderStatus.PENDING_SHOP_CONFIRMATION,
            OrderStatus.AWAITING_CUSTOMER_CONFIRMATION,
        }:
            raise OrderTransitionError(
                f"Order {order_id} cannot be amended in "
                f"{order.status.value} state."
            )
        amendment = order.add_amendment(description)
        self._save()
        return amendment

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_high_value(self, order: Order) -> bool:
        return order.total >= self.high_value_threshold

    def get_stale_pending_orders(self, max_age_minutes: int,
                                 tenant_id: Optional[str] = None) -> List[Order]:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=max_age_minutes)
        return [
            o for o in self.get_all_orders(tenant_id=tenant_id)
            if o.status == OrderStatus.PENDING_SHOP_CONFIRMATION and o.created_at <= cutoff
        ]

    def _get_or_raise(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Order {order_id!r} not found.")
        return order

    def _save(self) -> None:
        self.persistence.save(
            {
                "orders": [serialize_order(o) for o in self._orders.values()],
                "idempotency_index": self._idempotency_index,
            }
        )

    def _load(self) -> None:
        payload = self.persistence.load()
        orders_raw = payload.get("orders", [])
        self._orders = deserialize_orders(orders_raw)
        self._idempotency_index = payload.get("idempotency_index", {})
