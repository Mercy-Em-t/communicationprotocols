# communicationprotocols

Protocol for managing system communications internally and externally.

## Overview

This system manages the messaging workflow between **customers**, **businesses**, and an **operations** team in a multi-tenant ordering platform.

### Relationship model

| Relationship | Cardinality | Description |
|---|---|---|
| Customer ↔ Business | 1:1 per order | Each customer has one order thread per business |
| Business ↔ Customers | 1:n | A business handles many customers |
| Operations ↔ Businesses ↔ Customers | n:1:n | Operations monitors all businesses without intervening in every order |

---

## Architecture

```
src/
├── models.py               # Core data models (Customer, Business, Order, Message, Thread)
├── order_service.py        # Order lifecycle and state machine
├── messaging_service.py    # Templated messages, thread management, flood prevention
├── notification_service.py # Façade — coordinates the full message loop
└── operations_dashboard.py # Aggregated read-only view for operations

tests/
└── test_messaging.py       # Full test suite (26 tests)
```

---

## Key design decisions

### 1. One thread per order
Each order gets its own `MessageThread`. Customer and business messages for order `ORD-20260410-A1B2` are completely isolated from any other order, preventing mixed-order confusion.

### 2. Flood prevention — businesses don't get 500 WhatsApp messages
When 500 customers order at once the business receives **one structured `IN_APP` (dashboard) notification per order**, not a raw WhatsApp message. The business manages all orders from the dashboard.

```python
# All customer orders produce an IN_APP dashboard notification for the business
order = notification_svc.place_order(customer, business, items)
# → customer gets WhatsApp confirmation
# → business gets a single "[DASHBOARD] New order ORD-…" IN_APP message
```

### 3. Template messages
All customer-facing messages are pre-filled from templates:
```
Hi Mercy, your order ORD-20260410-A1B2 has been received.

  • Chia Seeds x2 — KES 400

Total: KES 400

You can reply with changes before it is processed.
```

### 4. Immutable amendment audit trail
Amendments are appended to the order and never modify existing records:
```python
order = notification_svc.amend_order(order_id, "Remove the chia seeds")
# order.amendments → [Amendment(description="Remove the chia seeds", ...)]
```

### 5. Operations — intervene only when necessary
Operations never see raw customer-business traffic. They receive alerts only for:
- High-value orders (≥ KES 5,000)
- Stock-out events
- Fraud / anomaly events

The `OperationsDashboard` provides an aggregated view per business (totals, statuses, revenue) and allows drill-down into individual order threads when investigation is needed.

### 6. Order state machine
```
PENDING → PROCESSED → DELIVERED
PENDING → CANCELLED
PROCESSED → CANCELLED
```
Invalid transitions raise `OrderTransitionError`.

### 7. Tenant isolation and RBAC
Core entities include `tenant_id`, and service-level checks enforce same-tenant data access. Role-aware actions are supported via `AuthContext` (`customer`, `business`, `operations`, `system`).

### 8. Idempotency and duplicate protection
`NotificationService.place_order(..., idempotency_key="...")` returns the original order for repeated keys, preventing duplicate order creation from repeated clicks/retries.

### 9. Inventory reservation with atomic checks
`InventoryService` supports atomic reserve/release to avoid overselling. Stock is reserved on order creation and released automatically when an order is cancelled.

### 10. Delivery reliability (outbox/retries/dead-letter)
Messages are tracked with delivery status, retry attempts, and dead-letter promotion after max retries. This provides a clear failure trail for WhatsApp/API outages.

### 11. SLA escalation + audit trail
`NotificationService.check_sla_breaches()` raises operations alerts for stale pending orders once per order, and key actions are written to immutable audit events with retention pruning.

---

## Quick start

```python
from src.models import Business, Customer, OrderItem, OrderStatus, NotificationTrigger
from src.notification_service import NotificationService
from src.operations_dashboard import OperationsDashboard

svc = NotificationService()
dashboard = OperationsDashboard(svc.orders, svc.messages)

# Create entities
customer = Customer.create("Mercy", "+254700000001")
business = Business.create("Healthy Eats", "+254711111111")
items = [OrderItem("Chia Seeds", 2, 200.0)]

# Customer places an order
order = svc.place_order(customer, business, items)
# → WhatsApp confirmation sent to customer
# → IN_APP dashboard notification sent to business (no WhatsApp flood)

# Customer amends the order
svc.amend_order(order.order_id, "No packaging, please")

# Business processes and delivers the order
svc.advance_order(order.order_id, OrderStatus.PROCESSED)
svc.advance_order(order.order_id, OrderStatus.DELIVERED)

# Operations views aggregated dashboard
report = dashboard.generate_report()
for summary in report.summaries:
    print(f"{summary.business_name}: {summary.total_orders} orders, "
          f"KES {summary.total_revenue:,.0f} revenue")

# Manual operations alert
svc.trigger_operations_alert(
    order.order_id,
    NotificationTrigger.STOCK_OUT,
    detail="Chia Seeds out of stock",
)

# Idempotent create (prevents duplicates)
same_order = svc.place_order(customer, business, items, idempotency_key="req-123")

# SLA check (alerts ops for stale pending orders)
svc.check_sla_breaches()
```

---

## Running tests

```bash
pip install -e ".[dev]"
pytest
```
