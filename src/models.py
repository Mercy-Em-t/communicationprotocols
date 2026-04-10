"""
Core data models for the communication protocol system.

Relationships:
- Customer ↔ Business: 1:1 per order, 1:n customers per business
- Operations ↔ Businesses ↔ Customers: n:1:n
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any


class OrderStatus(Enum):
    """Lifecycle states for an order."""
    CREATED = "created"
    PENDING_SHOP_CONFIRMATION = "pending_shop_confirmation"
    REJECTED_BY_SHOP = "rejected_by_shop"
    ACCEPTED_BY_SHOP = "accepted_by_shop"
    AWAITING_CUSTOMER_CONFIRMATION = "awaiting_customer_confirmation"
    AWAITING_PAYMENT = "awaiting_payment"
    PAYMENT_PROCESSING = "payment_processing"
    PAID = "paid"
    FULFILLING = "fulfilling"
    READY_FOR_PICKUP = "ready_for_pickup"
    OUT_FOR_DELIVERY = "out_for_delivery"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MessageChannel(Enum):
    """Supported notification channels."""
    WHATSAPP = "whatsapp"
    SMS = "sms"
    IN_APP = "in_app"
    EMAIL = "email"


class NotificationTrigger(Enum):
    """Events that drive operations alerts."""
    STOCK_OUT = "stock_out"
    FRAUD_ANOMALY = "fraud_anomaly"
    HIGH_VALUE_ORDER = "high_value_order"
    SLA_BREACH = "sla_breach"


class DeliveryStatus(Enum):
    """Delivery status for outbound messages."""
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class Customer:
    """A customer who can place orders with businesses."""
    customer_id: str
    name: str
    phone: str
    tenant_id: str = "default"
    preferred_channel: MessageChannel = MessageChannel.WHATSAPP

    @staticmethod
    def create(name: str, phone: str,
               preferred_channel: MessageChannel = MessageChannel.WHATSAPP,
               tenant_id: str = "default") -> "Customer":
        return Customer(
            customer_id=str(uuid.uuid4()),
            name=name,
            phone=phone,
            tenant_id=tenant_id,
            preferred_channel=preferred_channel,
        )


@dataclass
class Business:
    """A business that serves customers and is monitored by operations."""
    business_id: str
    name: str
    phone: str
    tenant_id: str = "default"

    @staticmethod
    def create(name: str, phone: str, tenant_id: str = "default") -> "Business":
        return Business(
            business_id=str(uuid.uuid4()),
            name=name,
            phone=phone,
            tenant_id=tenant_id,
        )


@dataclass
class OrderItem:
    """A single line item within an order."""
    product_name: str
    quantity: int
    unit_price: float

    @property
    def line_total(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class Amendment:
    """An immutable record of a change made to an order."""
    amendment_id: str
    order_id: str
    description: str
    changed_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @staticmethod
    def create(order_id: str, description: str) -> "Amendment":
        return Amendment(
            amendment_id=str(uuid.uuid4()),
            order_id=order_id,
            description=description,
        )


@dataclass
class Order:
    """
    An atomic order placed by one customer with one business.

    Each order has its own message thread and immutable audit trail.
    Status transitions are enforced by OrderService.
    """
    order_id: str
    customer: Customer
    business: Business
    tenant_id: str
    items: List[OrderItem]
    status: OrderStatus = OrderStatus.CREATED
    amendments: List[Amendment] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @staticmethod
    def create(customer: Customer, business: Business,
               items: List[OrderItem]) -> "Order":
        if customer.tenant_id != business.tenant_id:
            raise ValueError("Customer and business must belong to the same tenant.")
        now = datetime.now(tz=timezone.utc)
        date_str = now.strftime("%Y%m%d")
        short_id = str(uuid.uuid4())[:4].upper()
        order_id = f"ORD-{date_str}-{short_id}"
        return Order(
            order_id=order_id,
            customer=customer,
            business=business,
            tenant_id=customer.tenant_id,
            items=items,
            created_at=now,
            updated_at=now,
        )

    @property
    def total(self) -> float:
        return sum(item.line_total for item in self.items)

    def add_amendment(self, description: str) -> Amendment:
        """Record an immutable amendment and update the order timestamp."""
        amendment = Amendment.create(self.order_id, description)
        self.amendments.append(amendment)
        self.updated_at = datetime.now(tz=timezone.utc)
        return amendment


@dataclass
class Message:
    """A single message sent via a channel within an order thread."""
    message_id: str
    order_id: str
    sender: str          # "customer", "business", or "operations"
    recipient: str       # "customer", "business", or "operations"
    channel: MessageChannel
    body: str
    tenant_id: str
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    delivery_attempts: int = 0
    last_error: Optional[str] = None
    sent_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    delivered_at: Optional[datetime] = None

    @staticmethod
    def create(order_id: str, sender: str, recipient: str,
               channel: MessageChannel, body: str, tenant_id: str) -> "Message":
        return Message(
            message_id=str(uuid.uuid4()),
            order_id=order_id,
            sender=sender,
            recipient=recipient,
            channel=channel,
            body=body,
            tenant_id=tenant_id,
        )


@dataclass
class MessageThread:
    """
    One thread per order per customer-business pair.

    Keeps all messages scoped to a single order so neither side
    sees unrelated conversation history.
    """
    thread_id: str
    order_id: str
    tenant_id: str
    customer_id: str
    business_id: str
    messages: List[Message] = field(default_factory=list)

    @staticmethod
    def create(order: Order) -> "MessageThread":
        return MessageThread(
            thread_id=str(uuid.uuid4()),
            order_id=order.order_id,
            tenant_id=order.tenant_id,
            customer_id=order.customer.customer_id,
            business_id=order.business.business_id,
        )

    def add_message(self, message: Message) -> None:
        self.messages.append(message)


@dataclass
class OrderEvent:
    """Immutable order lifecycle event for reporting and traceability."""
    event_id: str
    order_id: str
    tenant_id: str
    event_type: str
    from_status: Optional[str]
    to_status: Optional[str]
    trigger: str
    actor_role: str
    actor_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @staticmethod
    def create(order_id: str, tenant_id: str, event_type: str, *,
               from_status: Optional[str], to_status: Optional[str],
               trigger: str, actor_role: str, actor_id: str,
               metadata: Optional[Dict[str, Any]] = None) -> "OrderEvent":
        return OrderEvent(
            event_id=str(uuid.uuid4()),
            order_id=order_id,
            tenant_id=tenant_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            trigger=trigger,
            actor_role=actor_role,
            actor_id=actor_id,
            metadata=metadata or {},
        )


@dataclass
class AuditEvent:
    """Immutable audit event for actions and access."""
    event_id: str
    tenant_id: str
    actor_role: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @staticmethod
    def create(tenant_id: str, actor_role: str, actor_id: str, action: str,
               resource_type: str, resource_id: str,
               metadata: Optional[Dict[str, Any]] = None) -> "AuditEvent":
        return AuditEvent(
            event_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            actor_role=actor_role,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata or {},
        )
