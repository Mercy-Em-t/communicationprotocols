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

from datetime import datetime, timezone
from typing import List, Optional, Set

from .auth import AuthContext, Role, authorize_order_action, authorize_order_read

from .models import (
    Business,
    Customer,
    NotificationTrigger,
    Order,
    OrderItem,
    OrderStatus,
    AuditEvent,
)
from .messaging_service import MessagingService
from .order_service import HIGH_VALUE_THRESHOLD, OrderService
from .persistence import (
    JsonPersistence,
    deserialize_audits,
    serialize_audit,
)


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
                 messaging_service: Optional[MessagingService] = None,
                 persistence_file: Optional[str] = None,
                 sla_minutes: int = 30) -> None:
        self.persistence = JsonPersistence(file_path=persistence_file)
        self.orders = order_service or OrderService(persistence=self.persistence)
        self.messages = messaging_service or MessagingService(persistence=self.persistence)
        self.sla_minutes = sla_minutes
        self._sla_alerted_order_ids: Set[str] = set()
        self._audits: List[AuditEvent] = []
        self._load()

    # ------------------------------------------------------------------
    # Customer actions
    # ------------------------------------------------------------------

    def place_order(self, customer: Customer, business: Business,
                    items: List[OrderItem], *,
                    idempotency_key: Optional[str] = None,
                    actor: Optional[AuthContext] = None) -> Order:
        """
        Customer places a new order.

        1. Order created and persisted.
        2. Confirmation sent to customer.
        3. Structured notification logged for the business dashboard.
        4. High-value alert sent to operations if applicable.
        """
        if actor is not None and actor.role == Role.CUSTOMER:
            if actor.actor_id != customer.customer_id or actor.tenant_id != customer.tenant_id:
                raise PermissionError("Customer context does not match order owner.")
        order = self.orders.create_order(
            customer,
            business,
            items,
            idempotency_key=idempotency_key,
            actor=actor,
        )
        self.messages.notify_order_received(order)
        self.messages.notify_business_new_order(order)
        self._audit(actor, order.tenant_id, "place_order", "order", order.order_id)

        if self.orders.is_high_value(order):
            self.messages.alert_operations(
                order,
                reason=f"High-value order (KES {order.total:,.0f} ≥ "
                       f"KES {HIGH_VALUE_THRESHOLD:,.0f})",
            )
            self._audit(actor, order.tenant_id, "high_value_alert", "order", order.order_id)
        return order

    def amend_order(self, order_id: str, description: str,
                    actor: Optional[AuthContext] = None) -> Order:
        """
        Customer requests an amendment to a PENDING order.

        The amendment is appended to the immutable audit trail and an
        acknowledgement is sent back to the customer.
        """
        order = self.orders._get_or_raise(order_id)
        authorize_order_action(
            actor, order, (Role.CUSTOMER, Role.BUSINESS, Role.OPERATIONS, Role.SYSTEM)
        )
        amendment = self.orders.amend_order(order_id, description, actor=actor)
        order = self.orders.get_order(order_id, actor=actor)
        self.messages.notify_amendment_received(order, amendment.description)
        self._audit(actor, order.tenant_id, "amend_order", "order", order.order_id)
        return order

    # ------------------------------------------------------------------
    # Business actions
    # ------------------------------------------------------------------

    def advance_order(self, order_id: str,
                      new_status: OrderStatus,
                      actor: Optional[AuthContext] = None) -> Order:
        """
        Business advances an order to the next status.

        A status-update notification is sent to the customer automatically.
        """
        order = self.orders._get_or_raise(order_id)
        authorize_order_action(actor, order, (Role.BUSINESS, Role.OPERATIONS, Role.SYSTEM))
        order = self.orders.advance_order(order_id, new_status, actor=actor)
        self.messages.notify_order_status(order)
        self._audit(actor, order.tenant_id, "advance_order", "order", order.order_id)
        return order

    # ------------------------------------------------------------------
    # Operations actions
    # ------------------------------------------------------------------

    def trigger_operations_alert(self, order_id: str,
                                   trigger: NotificationTrigger,
                                   detail: str = "",
                                   actor: Optional[AuthContext] = None) -> Order:
        """
        Manually fire an operations alert for a specific trigger.

        Operations normally only intervene when automatic thresholds are
        crossed, but this method allows explicit escalation.
        """
        order = self.orders._get_or_raise(order_id)
        authorize_order_action(actor, order, (Role.OPERATIONS, Role.BUSINESS, Role.SYSTEM))
        reason = f"{trigger.value}: {detail}" if detail else trigger.value
        self.messages.alert_operations(order, reason=reason)
        self._audit(actor, order.tenant_id, "trigger_operations_alert", "order", order.order_id)
        return order

    def check_sla_breaches(self, tenant_id: Optional[str] = None,
                           actor: Optional[AuthContext] = None) -> List[str]:
        stale_orders = self.orders.get_stale_pending_orders(
            max_age_minutes=self.sla_minutes,
            tenant_id=tenant_id,
        )
        alerted: List[str] = []
        for order in stale_orders:
            if order.order_id in self._sla_alerted_order_ids:
                continue
            self.messages.alert_operations(
                order, reason=f"{NotificationTrigger.SLA_BREACH.value}: pending beyond SLA"
            )
            self._sla_alerted_order_ids.add(order.order_id)
            alerted.append(order.order_id)
            self._audit(actor, order.tenant_id, "sla_breach_alert", "order", order.order_id)
        self._save()
        return alerted

    def list_audit_events(self, tenant_id: Optional[str] = None) -> List[AuditEvent]:
        if tenant_id is None:
            return list(self._audits)
        return [a for a in self._audits if a.tenant_id == tenant_id]

    def _audit(self, actor: Optional[AuthContext], tenant_id: str,
               action: str, resource_type: str, resource_id: str) -> None:
        role = actor.role.value if actor else Role.SYSTEM.value
        actor_id = actor.actor_id if actor else "system"
        event = AuditEvent.create(
            tenant_id=tenant_id,
            actor_role=role,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata={"at": datetime.now(tz=timezone.utc).isoformat()},
        )
        self._audits.append(event)
        self._audits = self.persistence.prune_audit(self._audits)
        self._save()

    def _save(self) -> None:
        self.persistence.save(
            {
                "audits": [serialize_audit(a) for a in self._audits],
                "sla_alerted_order_ids": list(self._sla_alerted_order_ids),
            }
        )

    def _load(self) -> None:
        payload = self.persistence.load()
        self._audits = deserialize_audits(payload.get("audits", []))
        self._sla_alerted_order_ids = set(payload.get("sla_alerted_order_ids", []))
