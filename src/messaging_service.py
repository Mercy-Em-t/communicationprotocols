"""
Messaging service — handles templated messages, thread management,
and routing across channels.

Design principles
-----------------
* One thread per order → no mixed-order confusion.
* Template messages pre-filled with order details → fewer errors.
* Businesses receive a single dashboard notification, not one WhatsApp
  message per customer order — flood prevention built in.
* All messages are logged through the system before reaching any channel
  so operations can observe without intervening.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import (
    Message,
    MessageChannel,
    MessageThread,
    Order,
    OrderStatus,
)


class MessagingService:
    """
    Routes messages between customers, businesses and operations.

    All traffic passes through ``send_message`` so that every message
    is logged in the correct thread before delivery simulation.
    """

    def __init__(self) -> None:
        # Indexed by order_id for O(1) thread lookup
        self._threads: Dict[str, MessageThread] = {}

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------

    def get_or_create_thread(self, order: Order) -> MessageThread:
        """Return the thread for this order, creating it if absent."""
        if order.order_id not in self._threads:
            self._threads[order.order_id] = MessageThread.create(order)
        return self._threads[order.order_id]

    def get_thread(self, order_id: str) -> Optional[MessageThread]:
        return self._threads.get(order_id)

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    def send_message(self, order: Order, sender: str,
                     recipient: str, body: str,
                     channel: Optional[MessageChannel] = None) -> Message:
        """
        Log and "send" a message on the order's thread.

        The channel defaults to the customer's preferred channel when the
        recipient is a customer, and WhatsApp for business/operations.
        """
        if channel is None:
            if recipient == "customer":
                channel = order.customer.preferred_channel
            else:
                channel = MessageChannel.WHATSAPP

        thread = self.get_or_create_thread(order)
        message = Message.create(
            order_id=order.order_id,
            sender=sender,
            recipient=recipient,
            channel=channel,
            body=body,
        )
        thread.add_message(message)
        self._deliver(message)
        return message

    # ------------------------------------------------------------------
    # Templated customer notifications
    # ------------------------------------------------------------------

    def notify_order_received(self, order: Order) -> Message:
        """
        Send confirmation to the customer.

        Template: "Hi {name}, your order {id} has been received…"
        """
        items_text = self._format_items_detailed(order)
        body = (
            f"Hi {order.customer.name}, your order {order.order_id} has been "
            f"received.\n\n{items_text}\n\nTotal: KES {order.total:,.0f}\n\n"
            "You can reply with changes before it is processed."
        )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_order_status(self, order: Order) -> Message:
        """Send a status-update notification to the customer."""
        status_messages = {
            OrderStatus.PROCESSED: (
                f"Your order {order.order_id} is being prepared."
            ),
            OrderStatus.DELIVERED: (
                f"Your order {order.order_id} has been delivered. "
                "Thank you for your purchase!"
            ),
            OrderStatus.CANCELLED: (
                f"Your order {order.order_id} has been cancelled. "
                "Please contact us if you have questions."
            ),
        }
        body = status_messages.get(
            order.status,
            f"Update on your order {order.order_id}: {order.status.value}.",
        )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_amendment_received(self, order: Order,
                                  description: str) -> Message:
        """Acknowledge an amendment request back to the customer."""
        body = (
            f"Hi {order.customer.name}, we have received your amendment "
            f"request for order {order.order_id}: \"{description}\". "
            "We will update you shortly."
        )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    # ------------------------------------------------------------------
    # Business dashboard notification (flood prevention)
    # ------------------------------------------------------------------

    def notify_business_new_order(self, order: Order) -> Message:
        """
        Notify the business of a new order via the dashboard channel.

        Businesses receive one structured notification logged to the
        system rather than a raw WhatsApp message per order, keeping
        the business WhatsApp inbox from being flooded.
        """
        items_text = self._format_items_summary(order)
        body = (
            f"[DASHBOARD] New order {order.order_id} from "
            f"{order.customer.name} ({order.customer.phone}). "
            f"Items: {items_text}. Total: KES {order.total:,.0f}."
        )
        return self.send_message(order, sender="system",
                                 recipient="business", body=body,
                                 channel=MessageChannel.IN_APP)

    # ------------------------------------------------------------------
    # Operations alerts (intervene only when necessary)
    # ------------------------------------------------------------------

    def alert_operations(self, order: Order, reason: str) -> Message:
        """
        Send an alert to operations — only called when intervention is needed.

        Normal customer-business traffic is invisible to operations.
        """
        body = (
            f"[OPS ALERT] Order {order.order_id} | "
            f"Business: {order.business.name} | "
            f"Customer: {order.customer.name} | "
            f"Total: KES {order.total:,.0f} | "
            f"Reason: {reason}"
        )
        return self.send_message(order, sender="system",
                                 recipient="operations", body=body,
                                 channel=MessageChannel.IN_APP)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_items_detailed(order: Order) -> str:
        """Multi-line item list with quantities and per-line totals."""
        return "\n".join(
            f"  • {item.product_name} x{item.quantity} — "
            f"KES {item.line_total:,.0f}"
            for item in order.items
        )

    @staticmethod
    def _format_items_summary(order: Order) -> str:
        """Compact single-line item list for dashboard notifications."""
        return ", ".join(
            f"{item.product_name} x{item.quantity}"
            for item in order.items
        )

    # ------------------------------------------------------------------
    # Delivery simulation
    # ------------------------------------------------------------------

    @staticmethod
    def _deliver(message: Message) -> None:
        """
        Simulate delivery over the message's channel.

        In production this would call the WhatsApp Business API, SMS
        gateway, push notification service, etc.
        """
        # No-op in this implementation — real integrations plug in here.
        pass
