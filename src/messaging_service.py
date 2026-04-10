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

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from .models import (
    Message,
    MessageChannel,
    MessageThread,
    Order,
    OrderStatus,
    DeliveryStatus,
)
from .persistence import JsonPersistence, deserialize_threads, serialize_thread


class MessagingService:
    """
    Routes messages between customers, businesses and operations.

    All traffic passes through ``send_message`` so that every message
    is logged in the correct thread before delivery simulation.
    """

    def __init__(self, persistence: Optional[JsonPersistence] = None,
                 max_delivery_attempts: int = 3,
                 fail_channels: Optional[Set[MessageChannel]] = None) -> None:
        # Indexed by order_id for O(1) thread lookup
        self._threads: Dict[str, MessageThread] = {}
        self.persistence = persistence or JsonPersistence()
        self.max_delivery_attempts = max_delivery_attempts
        self.fail_channels = fail_channels or set()
        self._outbox: List[str] = []  # message ids
        self._dead_letters: List[str] = []  # message ids
        self._load()

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------

    def get_or_create_thread(self, order: Order) -> MessageThread:
        """Return the thread for this order, creating it if absent."""
        if order.order_id not in self._threads:
            self._threads[order.order_id] = MessageThread.create(order)
        return self._threads[order.order_id]

    def get_thread(self, order_id: str, tenant_id: Optional[str] = None) -> Optional[MessageThread]:
        thread = self._threads.get(order_id)
        if thread is None:
            return None
        if tenant_id is not None and thread.tenant_id != tenant_id:
            return None
        return thread

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
            tenant_id=order.tenant_id,
        )
        thread.add_message(message)
        self._outbox.append(message.message_id)
        self._dispatch_outbox(order.order_id)
        self._save()
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
            "We have sent it to the shop for confirmation."
        )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_shop_decision_request(self, order: Order) -> Message:
        items_text = self._format_items_summary(order)
        body = (
            f"Order {order.order_id} from {order.customer.name}. "
            f"Items: {items_text}. Reply 1 to accept or 2 to reject."
        )
        return self.send_message(order, sender="system",
                                 recipient="business", body=body,
                                 channel=MessageChannel.WHATSAPP)

    def notify_shop_decision_outcome(self, order: Order) -> Message:
        if order.status == OrderStatus.ACCEPTED_BY_SHOP:
            body = (
                f"Good news — shop accepted order {order.order_id}. "
                "Please confirm your items to proceed."
            )
        else:
            body = (
                f"Sorry — shop rejected order {order.order_id}. "
                "No payment will be requested."
            )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_customer_confirmation_request(self, order: Order) -> Message:
        body = (
            f"Please confirm order {order.order_id}. "
            "Reply 1 to confirm items or 2 to cancel."
        )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_payment_prompt(self, order: Order) -> Message:
        body = (
            f"Order {order.order_id} is ready for payment. "
            f"Please pay KES {order.total:,.0f} via M-Pesa."
        )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_payment_processing(self, order: Order) -> Message:
        body = f"Payment for order {order.order_id} is processing."
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_payment_result(self, order: Order, success: bool) -> Message:
        if success:
            body = f"Payment confirmed for order {order.order_id}. Thank you."
        else:
            body = (
                f"Payment failed for order {order.order_id}. "
                "Please retry payment."
            )
        return self.send_message(order, sender="business",
                                 recipient="customer", body=body)

    def notify_order_status(self, order: Order) -> Message:
        """Send a status-update notification to the customer."""
        status_messages = {
            OrderStatus.FULFILLING: (
                f"Your order {order.order_id} is being prepared."
            ),
            OrderStatus.READY_FOR_PICKUP: (
                f"Your order {order.order_id} is ready for pickup."
            ),
            OrderStatus.OUT_FOR_DELIVERY: (
                f"Your order {order.order_id} is out for delivery."
            ),
            OrderStatus.COMPLETED: (
                f"Your order {order.order_id} is completed. Thank you!"
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

    def _dispatch_outbox(self, order_id: Optional[str] = None) -> None:
        targets = []
        for thread in self._threads.values():
            if order_id is not None and thread.order_id != order_id:
                continue
            targets.extend(thread.messages)

        message_lookup = {m.message_id: m for m in targets}
        remaining: List[str] = []
        for message_id in self._outbox:
            message = message_lookup.get(message_id)
            if message is None:
                continue
            if message.delivery_status in {DeliveryStatus.DELIVERED, DeliveryStatus.DEAD_LETTER}:
                continue
            try:
                self._attempt_delivery(message)
            except RuntimeError as exc:
                message.last_error = str(exc)
                if message.delivery_attempts >= self.max_delivery_attempts:
                    message.delivery_status = DeliveryStatus.DEAD_LETTER
                    self._dead_letters.append(message.message_id)
                else:
                    message.delivery_status = DeliveryStatus.FAILED
                    remaining.append(message.message_id)
        self._outbox = remaining

    def _attempt_delivery(self, message: Message) -> None:
        message.delivery_attempts += 1
        if message.channel in self.fail_channels:
            raise RuntimeError(f"Channel {message.channel.value} unavailable.")
        message.delivery_status = DeliveryStatus.DELIVERED
        message.delivered_at = datetime.now(tz=timezone.utc)
        message.last_error = None

    def get_dead_letters(self) -> List[Message]:
        dead_ids = set(self._dead_letters)
        out: List[Message] = []
        for thread in self._threads.values():
            for msg in thread.messages:
                if msg.message_id in dead_ids:
                    out.append(msg)
        return out

    def get_message_by_id(self, message_id: str) -> Optional[Message]:
        for thread in self._threads.values():
            for msg in thread.messages:
                if msg.message_id == message_id:
                    return msg
        return None

    def process_delivery_callback(self, message_id: str, *,
                                  delivered: bool,
                                  error: Optional[str] = None) -> Optional[Message]:
        message = self.get_message_by_id(message_id)
        if message is None:
            return None
        if delivered:
            message.delivery_status = DeliveryStatus.DELIVERED
            message.delivered_at = datetime.now(tz=timezone.utc)
            message.last_error = None
        else:
            message.delivery_status = DeliveryStatus.FAILED
            message.last_error = error or "Delivery callback marked failed."
            if message.delivery_attempts >= self.max_delivery_attempts:
                message.delivery_status = DeliveryStatus.DEAD_LETTER
                self._dead_letters.append(message.message_id)
        self._save()
        return message

    def _save(self) -> None:
        self.persistence.save(
            {
                "threads": [serialize_thread(t) for t in self._threads.values()],
                "outbox": self._outbox,
                "dead_letters": self._dead_letters,
            }
        )

    def _load(self) -> None:
        payload = self.persistence.load()
        self._threads = deserialize_threads(payload.get("threads", []))
        self._outbox = payload.get("outbox", [])
        self._dead_letters = payload.get("dead_letters", [])
