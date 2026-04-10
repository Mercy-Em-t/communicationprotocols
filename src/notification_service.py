"""
Notification service — orchestrates lifecycle transitions and messaging.

WhatsApp is a communication channel. Order state in backend is source of truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from .auth import AuthContext, Role, authorize_order_action
from .messaging_service import MessagingService
from .models import (
    AuditEvent,
    Business,
    Customer,
    NotificationTrigger,
    Order,
    OrderEvent,
    OrderItem,
    OrderStatus,
)
from .order_service import HIGH_VALUE_THRESHOLD, OrderService, OrderTransitionError
from .persistence import (
    JsonPersistence,
    deserialize_audits,
    deserialize_order_events,
    serialize_audit,
    serialize_order_event,
)


class NotificationService:
    """Façade that coordinates strict order lifecycle + communication."""

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
        self._order_events: List[OrderEvent] = []
        self._seen_whatsapp_message_ids: Set[str] = set()
        self._payment_reference_index: Dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # Customer checkout and amendments
    # ------------------------------------------------------------------

    def place_order(self, customer: Customer, business: Business,
                    items: List[OrderItem], *,
                    idempotency_key: Optional[str] = None,
                    actor: Optional[AuthContext] = None) -> Order:
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

        self._record_order_event(
            order,
            event_type="ORDER_CREATED",
            from_status=None,
            to_status=OrderStatus.CREATED,
            trigger="checkout",
            actor=actor,
        )

        if order.status == OrderStatus.CREATED:
            order = self._transition_with_event(
                order_id=order.order_id,
                new_status=OrderStatus.PENDING_SHOP_CONFIRMATION,
                event_type="ORDER_SUBMITTED_TO_SHOP",
                trigger="checkout",
                actor=actor,
            )

        self.messages.notify_order_received(order)
        self.messages.notify_business_new_order(order)
        self.messages.notify_shop_decision_request(order)
        self._audit(actor, order.tenant_id, "place_order", "order", order.order_id)

        if self.orders.is_high_value(order):
            self.messages.alert_operations(
                order,
                reason=f"High-value order (KES {order.total:,.0f} ≥ "
                       f"KES {HIGH_VALUE_THRESHOLD:,.0f})",
            )
            self._record_order_event(
                order,
                event_type="ORDER_HIGH_VALUE_ALERTED",
                from_status=order.status,
                to_status=order.status,
                trigger="high_value_threshold",
                actor=actor,
            )
            self._audit(actor, order.tenant_id, "high_value_alert", "order", order.order_id)

        return order

    def amend_order(self, order_id: str, description: str,
                    actor: Optional[AuthContext] = None) -> Order:
        amendment = self.orders.amend_order(order_id, description, actor=actor)
        order = self.orders.get_order_or_raise(order_id)
        self.messages.notify_amendment_received(order, amendment.description)
        self._record_order_event(
            order,
            event_type="ORDER_AMENDED",
            from_status=order.status,
            to_status=order.status,
            trigger="customer_amendment",
            actor=actor,
            metadata={"description": amendment.description},
        )
        self._audit(actor, order.tenant_id, "amend_order", "order", order.order_id)
        return order

    # ------------------------------------------------------------------
    # Shop decisions and customer confirmation
    # ------------------------------------------------------------------

    def shop_accept_order(self, order_id: str,
                          actor: Optional[AuthContext] = None) -> Order:
        order = self.orders.get_order_or_raise(order_id)
        authorize_order_action(actor, order, (Role.BUSINESS, Role.OPERATIONS, Role.SYSTEM))
        if order.status != OrderStatus.PENDING_SHOP_CONFIRMATION:
            raise OrderTransitionError("Order is not awaiting shop confirmation.")

        order = self._transition_with_event(
            order_id,
            OrderStatus.ACCEPTED_BY_SHOP,
            event_type="ORDER_ACCEPTED_BY_SHOP",
            trigger="shop_accept",
            actor=actor,
        )
        self.messages.notify_shop_decision_outcome(order)

        order = self._transition_with_event(
            order_id,
            OrderStatus.AWAITING_CUSTOMER_CONFIRMATION,
            event_type="ORDER_AWAITING_CUSTOMER_CONFIRMATION",
            trigger="post_shop_accept",
            actor=actor,
        )
        self.messages.notify_customer_confirmation_request(order)
        self._audit(actor, order.tenant_id, "shop_accept_order", "order", order.order_id)
        return order

    def shop_reject_order(self, order_id: str,
                          actor: Optional[AuthContext] = None) -> Order:
        order = self.orders.get_order_or_raise(order_id)
        authorize_order_action(actor, order, (Role.BUSINESS, Role.OPERATIONS, Role.SYSTEM))
        if order.status != OrderStatus.PENDING_SHOP_CONFIRMATION:
            raise OrderTransitionError("Order is not awaiting shop confirmation.")

        order = self._transition_with_event(
            order_id,
            OrderStatus.REJECTED_BY_SHOP,
            event_type="ORDER_REJECTED_BY_SHOP",
            trigger="shop_reject",
            actor=actor,
        )
        self.messages.notify_shop_decision_outcome(order)
        self._audit(actor, order.tenant_id, "shop_reject_order", "order", order.order_id)
        return order

    def customer_confirm_items(self, order_id: str,
                               actor: Optional[AuthContext] = None) -> Order:
        order = self.orders.get_order_or_raise(order_id)
        authorize_order_action(actor, order, (Role.CUSTOMER, Role.OPERATIONS, Role.SYSTEM))
        if order.status != OrderStatus.AWAITING_CUSTOMER_CONFIRMATION:
            raise OrderTransitionError("Order is not awaiting customer confirmation.")

        order = self._transition_with_event(
            order_id,
            OrderStatus.AWAITING_PAYMENT,
            event_type="ORDER_AWAITING_PAYMENT",
            trigger="customer_confirm_items",
            actor=actor,
        )
        self.messages.notify_payment_prompt(order)
        self._audit(actor, order.tenant_id, "customer_confirm_items", "order", order.order_id)
        return order

    # ------------------------------------------------------------------
    # Payment flow
    # ------------------------------------------------------------------

    def start_payment(self, order_id: str,
                      actor: Optional[AuthContext] = None) -> Order:
        order = self.orders.get_order_or_raise(order_id)
        authorize_order_action(actor, order, (Role.CUSTOMER, Role.OPERATIONS, Role.SYSTEM))
        if order.status != OrderStatus.AWAITING_PAYMENT:
            raise OrderTransitionError("Order is not awaiting payment.")

        order = self._transition_with_event(
            order_id,
            OrderStatus.PAYMENT_PROCESSING,
            event_type="ORDER_PAYMENT_PROCESSING",
            trigger="payment_initiated",
            actor=actor,
        )
        self.messages.notify_payment_processing(order)
        self._audit(actor, order.tenant_id, "start_payment", "order", order.order_id)
        return order

    def confirm_payment_webhook(self, order_id: str, payment_reference: str,
                                success: bool, *,
                                actor: Optional[AuthContext] = None) -> Order:
        order = self.orders.get_order_or_raise(order_id)
        authorize_order_action(actor, order, (Role.SYSTEM, Role.OPERATIONS))

        existing = self._payment_reference_index.get(payment_reference)
        if existing is not None:
            if existing != order_id:
                raise ValueError("Payment reference already bound to another order.")
            return order

        if order.status != OrderStatus.PAYMENT_PROCESSING:
            raise OrderTransitionError("Order is not in payment processing state.")

        target = OrderStatus.PAID if success else OrderStatus.AWAITING_PAYMENT
        event_type = "ORDER_PAID" if success else "ORDER_PAYMENT_FAILED"
        trigger = "payment_webhook_success" if success else "payment_webhook_failed"

        order = self._transition_with_event(
            order_id,
            target,
            event_type=event_type,
            trigger=trigger,
            actor=actor,
            metadata={"payment_reference": payment_reference},
        )
        self._payment_reference_index[payment_reference] = order_id
        self.messages.notify_payment_result(order, success=success)
        self._audit(actor, order.tenant_id, "confirm_payment_webhook", "order", order.order_id)
        self._save()
        return order

    # ------------------------------------------------------------------
    # Fulfillment
    # ------------------------------------------------------------------

    def start_fulfillment(self, order_id: str,
                          actor: Optional[AuthContext] = None) -> Order:
        return self._advance_with_customer_notification(
            order_id,
            OrderStatus.FULFILLING,
            event_type="ORDER_FULFILLING",
            trigger="shop_start_fulfillment",
            actor=actor,
        )

    def mark_ready_for_pickup(self, order_id: str,
                              actor: Optional[AuthContext] = None) -> Order:
        return self._advance_with_customer_notification(
            order_id,
            OrderStatus.READY_FOR_PICKUP,
            event_type="ORDER_READY_FOR_PICKUP",
            trigger="shop_ready_for_pickup",
            actor=actor,
        )

    def mark_out_for_delivery(self, order_id: str,
                              actor: Optional[AuthContext] = None) -> Order:
        return self._advance_with_customer_notification(
            order_id,
            OrderStatus.OUT_FOR_DELIVERY,
            event_type="ORDER_OUT_FOR_DELIVERY",
            trigger="shop_out_for_delivery",
            actor=actor,
        )

    def complete_order(self, order_id: str,
                       actor: Optional[AuthContext] = None) -> Order:
        return self._advance_with_customer_notification(
            order_id,
            OrderStatus.COMPLETED,
            event_type="ORDER_COMPLETED",
            trigger="order_completed",
            actor=actor,
        )

    def cancel_order(self, order_id: str,
                     actor: Optional[AuthContext] = None) -> Order:
        return self._advance_with_customer_notification(
            order_id,
            OrderStatus.CANCELLED,
            event_type="ORDER_CANCELLED",
            trigger="order_cancelled",
            actor=actor,
        )

    def advance_order(self, order_id: str,
                      new_status: OrderStatus,
                      actor: Optional[AuthContext] = None) -> Order:
        return self._advance_with_customer_notification(
            order_id,
            new_status,
            event_type=f"ORDER_{new_status.name}",
            trigger="manual_advance",
            actor=actor,
        )

    # ------------------------------------------------------------------
    # WhatsApp inbound mapping and delivery callbacks
    # ------------------------------------------------------------------

    def handle_incoming_whatsapp(self, *, from_phone: str, order_id: str,
                                 message: str, message_id: str,
                                 timestamp: datetime,
                                 signature_valid: bool = True) -> str:
        if not signature_valid:
            raise PermissionError("Invalid WhatsApp webhook signature.")
        if message_id in self._seen_whatsapp_message_ids:
            return "duplicate_ignored"

        if (datetime.now(tz=timezone.utc) - timestamp).total_seconds() > 300:
            raise PermissionError("Webhook timestamp is too old.")

        order = self.orders.get_order_or_raise(order_id)
        normalized = message.strip()

        if from_phone == order.business.phone:
            actor = AuthContext(role=Role.BUSINESS, actor_id=order.business.business_id,
                                tenant_id=order.tenant_id)
            if order.status == OrderStatus.PENDING_SHOP_CONFIRMATION and normalized == "1":
                self.shop_accept_order(order_id, actor=actor)
                action = "SHOP_ACCEPT_ORDER"
            elif order.status == OrderStatus.PENDING_SHOP_CONFIRMATION and normalized == "2":
                self.shop_reject_order(order_id, actor=actor)
                action = "SHOP_REJECT_ORDER"
            elif order.status == OrderStatus.PAID and normalized == "1":
                self.start_fulfillment(order_id, actor=actor)
                action = "SHOP_START_FULFILLMENT"
            else:
                raise ValueError("Unsupported business action for current order state.")
        elif from_phone == order.customer.phone:
            actor = AuthContext(role=Role.CUSTOMER, actor_id=order.customer.customer_id,
                                tenant_id=order.tenant_id)
            if order.status == OrderStatus.AWAITING_CUSTOMER_CONFIRMATION and normalized == "1":
                self.customer_confirm_items(order_id, actor=actor)
                action = "CUSTOMER_CONFIRM_ITEMS"
            elif order.status == OrderStatus.AWAITING_CUSTOMER_CONFIRMATION and normalized == "2":
                self.cancel_order(order_id, actor=actor)
                action = "CUSTOMER_CANCEL_ORDER"
            elif order.status == OrderStatus.AWAITING_PAYMENT and normalized == "1":
                self.start_payment(order_id, actor=actor)
                action = "CUSTOMER_START_PAYMENT"
            else:
                raise ValueError("Unsupported customer action for current order state.")
        else:
            raise PermissionError("Incoming phone is not bound to this order context.")

        self._seen_whatsapp_message_ids.add(message_id)
        self._save()
        return action

    def process_delivery_status_callback(self, message_id: str, *, delivered: bool,
                                         error: Optional[str] = None) -> None:
        self.messages.process_delivery_callback(
            message_id,
            delivered=delivered,
            error=error,
        )

    # ------------------------------------------------------------------
    # Operations and reporting
    # ------------------------------------------------------------------

    def trigger_operations_alert(self, order_id: str,
                                 trigger: NotificationTrigger,
                                 detail: str = "",
                                 actor: Optional[AuthContext] = None) -> Order:
        order = self.orders.get_order_or_raise(order_id)
        authorize_order_action(actor, order, (Role.OPERATIONS, Role.BUSINESS, Role.SYSTEM))
        reason = f"{trigger.value}: {detail}" if detail else trigger.value
        self.messages.alert_operations(order, reason=reason)
        self._record_order_event(
            order,
            event_type="ORDER_OPERATIONS_ALERT",
            from_status=order.status,
            to_status=order.status,
            trigger=trigger.value,
            actor=actor,
            metadata={"detail": detail},
        )
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
            self._record_order_event(
                order,
                event_type="ORDER_SLA_BREACH_ALERT",
                from_status=order.status,
                to_status=order.status,
                trigger=NotificationTrigger.SLA_BREACH.value,
                actor=actor,
            )
            self._audit(actor, order.tenant_id, "sla_breach_alert", "order", order.order_id)
        self._save()
        return alerted

    def list_audit_events(self, tenant_id: Optional[str] = None) -> List[AuditEvent]:
        if tenant_id is None:
            return list(self._audits)
        return [a for a in self._audits if a.tenant_id == tenant_id]

    def list_order_events(self, *, order_id: Optional[str] = None,
                          tenant_id: Optional[str] = None) -> List[OrderEvent]:
        events = list(self._order_events)
        if order_id is not None:
            events = [e for e in events if e.order_id == order_id]
        if tenant_id is not None:
            events = [e for e in events if e.tenant_id == tenant_id]
        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _advance_with_customer_notification(self, order_id: str,
                                            new_status: OrderStatus,
                                            *,
                                            event_type: str,
                                            trigger: str,
                                            actor: Optional[AuthContext]) -> Order:
        order = self.orders.get_order_or_raise(order_id)
        authorize_order_action(actor, order, (Role.BUSINESS, Role.OPERATIONS, Role.SYSTEM, Role.CUSTOMER))

        order = self._transition_with_event(
            order_id,
            new_status,
            event_type=event_type,
            trigger=trigger,
            actor=actor,
        )
        self.messages.notify_order_status(order)
        self._audit(actor, order.tenant_id, "advance_order", "order", order.order_id)
        return order

    def _transition_with_event(self, order_id: str,
                               new_status: OrderStatus,
                               *,
                               event_type: str,
                               trigger: str,
                               actor: Optional[AuthContext],
                               metadata: Optional[Dict[str, str]] = None) -> Order:
        existing = self.orders.get_order_or_raise(order_id)
        from_status = existing.status
        updated = self.orders.advance_order(order_id, new_status, actor=actor)
        self._record_order_event(
            updated,
            event_type=event_type,
            from_status=from_status,
            to_status=updated.status,
            trigger=trigger,
            actor=actor,
            metadata=metadata,
        )
        return updated

    def _record_order_event(self, order: Order, *,
                            event_type: str,
                            from_status: Optional[OrderStatus],
                            to_status: Optional[OrderStatus],
                            trigger: str,
                            actor: Optional[AuthContext],
                            metadata: Optional[Dict[str, str]] = None) -> None:
        role = actor.role.value if actor else Role.SYSTEM.value
        actor_id = actor.actor_id if actor else "system"
        event = OrderEvent.create(
            order_id=order.order_id,
            tenant_id=order.tenant_id,
            event_type=event_type,
            from_status=from_status.value if isinstance(from_status, OrderStatus) else None,
            to_status=to_status.value if isinstance(to_status, OrderStatus) else None,
            trigger=trigger,
            actor_role=role,
            actor_id=actor_id,
            metadata=metadata or {},
        )
        self._order_events.append(event)
        self._save()

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
                "order_events": [serialize_order_event(e) for e in self._order_events],
                "sla_alerted_order_ids": list(self._sla_alerted_order_ids),
                "seen_whatsapp_message_ids": list(self._seen_whatsapp_message_ids),
                "payment_reference_index": self._payment_reference_index,
            }
        )

    def _load(self) -> None:
        payload = self.persistence.load()
        self._audits = deserialize_audits(payload.get("audits", []))
        self._order_events = deserialize_order_events(payload.get("order_events", []))
        self._sla_alerted_order_ids = set(payload.get("sla_alerted_order_ids", []))
        self._seen_whatsapp_message_ids = set(payload.get("seen_whatsapp_message_ids", []))
        self._payment_reference_index = payload.get("payment_reference_index", {})
