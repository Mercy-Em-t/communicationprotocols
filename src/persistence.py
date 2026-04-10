from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    Amendment,
    AuditEvent,
    Business,
    Customer,
    DeliveryStatus,
    Message,
    MessageChannel,
    MessageThread,
    Order,
    OrderItem,
    OrderStatus,
)


def _dt(v: str) -> datetime:
    return datetime.fromisoformat(v)


def _to_iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_to_iso(x) for x in value]
    if isinstance(value, dict):
        return {k: _to_iso(v) for k, v in value.items()}
    return value


def _customer_from_dict(d: Dict[str, Any]) -> Customer:
    return Customer(
        customer_id=d["customer_id"],
        name=d["name"],
        phone=d["phone"],
        tenant_id=d.get("tenant_id", "default"),
        preferred_channel=MessageChannel(d["preferred_channel"]),
    )


def _business_from_dict(d: Dict[str, Any]) -> Business:
    return Business(
        business_id=d["business_id"],
        name=d["name"],
        phone=d["phone"],
        tenant_id=d.get("tenant_id", "default"),
    )


def _order_from_dict(d: Dict[str, Any]) -> Order:
    customer_payload = d.get("customer", {})
    customer_tenant = "default"
    if isinstance(customer_payload, dict):
        customer_tenant = customer_payload.get("tenant_id", "default")
    tenant_id = d.get("tenant_id", customer_tenant)

    amendments = [
        Amendment(
            amendment_id=a["amendment_id"],
            order_id=a["order_id"],
            description=a["description"],
            changed_at=_dt(a["changed_at"]),
        )
        for a in d.get("amendments", [])
    ]
    return Order(
        order_id=d["order_id"],
        customer=_customer_from_dict(d["customer"]),
        business=_business_from_dict(d["business"]),
        tenant_id=tenant_id,
        items=[OrderItem(**i) for i in d["items"]],
        status=OrderStatus(d["status"]),
        amendments=amendments,
        created_at=_dt(d["created_at"]),
        updated_at=_dt(d["updated_at"]),
    )


def _message_from_dict(d: Dict[str, Any]) -> Message:
    return Message(
        message_id=d["message_id"],
        order_id=d["order_id"],
        sender=d["sender"],
        recipient=d["recipient"],
        channel=MessageChannel(d["channel"]),
        body=d["body"],
        tenant_id=d.get("tenant_id", "default"),
        delivery_status=DeliveryStatus(d.get("delivery_status", "pending")),
        delivery_attempts=d.get("delivery_attempts", 0),
        last_error=d.get("last_error"),
        sent_at=_dt(d["sent_at"]),
        delivered_at=_dt(d["delivered_at"]) if d.get("delivered_at") else None,
    )


def _thread_from_dict(d: Dict[str, Any]) -> MessageThread:
    return MessageThread(
        thread_id=d["thread_id"],
        order_id=d["order_id"],
        tenant_id=d.get("tenant_id", "default"),
        customer_id=d["customer_id"],
        business_id=d["business_id"],
        messages=[_message_from_dict(m) for m in d.get("messages", [])],
    )


def _audit_from_dict(d: Dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_id=d["event_id"],
        tenant_id=d["tenant_id"],
        actor_role=d["actor_role"],
        actor_id=d["actor_id"],
        action=d["action"],
        resource_type=d["resource_type"],
        resource_id=d["resource_id"],
        metadata=d.get("metadata", {}),
        created_at=_dt(d["created_at"]),
    )


class JsonPersistence:
    """
    Tiny JSON-based persistence for this prototype.

    ``retention_days`` is applied to audit events via ``prune_audit``.
    Events older than that threshold are removed when new audit writes occur.
    """

    def __init__(self, file_path: Optional[str] = None,
                 retention_days: int = 90) -> None:
        """Create store; ``retention_days`` controls audit-event retention only."""
        self.path = Path(file_path) if file_path else None
        self.retention_days = retention_days

    def load(self) -> Dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: Dict[str, Any]) -> None:
        if self.path is None:
            return
        existing = self.load()
        existing.update(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(_to_iso(existing), f, indent=2, sort_keys=True)

    def prune_audit(self, audits: List[AuditEvent]) -> List[AuditEvent]:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.retention_days)
        return [a for a in audits if a.created_at >= cutoff]


def serialize_order(order: Order) -> Dict[str, Any]:
    return asdict(order)


def deserialize_orders(payload: List[Dict[str, Any]]) -> Dict[str, Order]:
    orders = [_order_from_dict(o) for o in payload]
    return {o.order_id: o for o in orders}


def serialize_thread(thread: MessageThread) -> Dict[str, Any]:
    return asdict(thread)


def deserialize_threads(payload: List[Dict[str, Any]]) -> Dict[str, MessageThread]:
    threads = [_thread_from_dict(t) for t in payload]
    return {t.order_id: t for t in threads}


def serialize_audit(event: AuditEvent) -> Dict[str, Any]:
    return asdict(event)


def deserialize_audits(payload: List[Dict[str, Any]]) -> List[AuditEvent]:
    return [_audit_from_dict(a) for a in payload]
