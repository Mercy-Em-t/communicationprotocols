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
from typing import List, Optional


class OrderStatus(Enum):
    """Lifecycle states for an order."""
    PENDING = "pending"
    PROCESSED = "processed"
    DELIVERED = "delivered"
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


@dataclass
class Customer:
    """A customer who can place orders with businesses."""
    customer_id: str
    name: str
    phone: str
    preferred_channel: MessageChannel = MessageChannel.WHATSAPP

    @staticmethod
    def create(name: str, phone: str,
               preferred_channel: MessageChannel = MessageChannel.WHATSAPP) -> "Customer":
        return Customer(
            customer_id=str(uuid.uuid4()),
            name=name,
            phone=phone,
            preferred_channel=preferred_channel,
        )


@dataclass
class Business:
    """A business that serves customers and is monitored by operations."""
    business_id: str
    name: str
    phone: str

    @staticmethod
    def create(name: str, phone: str) -> "Business":
        return Business(
            business_id=str(uuid.uuid4()),
            name=name,
            phone=phone,
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
    changed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

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
    Status transitions: PENDING → PROCESSED → DELIVERED (or CANCELLED).
    """
    order_id: str
    customer: Customer
    business: Business
    items: List[OrderItem]
    status: OrderStatus = OrderStatus.PENDING
    amendments: List[Amendment] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def create(customer: Customer, business: Business,
               items: List[OrderItem]) -> "Order":
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d")
        short_id = str(uuid.uuid4())[:4].upper()
        order_id = f"ORD-{date_str}-{short_id}"
        return Order(
            order_id=order_id,
            customer=customer,
            business=business,
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
        self.updated_at = datetime.now(timezone.utc)
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
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def create(order_id: str, sender: str, recipient: str,
               channel: MessageChannel, body: str) -> "Message":
        return Message(
            message_id=str(uuid.uuid4()),
            order_id=order_id,
            sender=sender,
            recipient=recipient,
            channel=channel,
            body=body,
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
    customer_id: str
    business_id: str
    messages: List[Message] = field(default_factory=list)

    @staticmethod
    def create(order: Order) -> "MessageThread":
        return MessageThread(
            thread_id=str(uuid.uuid4()),
            order_id=order.order_id,
            customer_id=order.customer.customer_id,
            business_id=order.business.business_id,
        )

    def add_message(self, message: Message) -> None:
        self.messages.append(message)
