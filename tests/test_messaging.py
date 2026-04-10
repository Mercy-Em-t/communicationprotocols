"""
Tests for the communication protocol system.

Covers:
- Customer ↔ Business messaging (1:1 / 1:n)
- Operations ↔ Businesses ↔ Customers (n:1:n)
- Order state machine
- Thread isolation (one thread per order)
- Flood prevention (business receives dashboard notifications, not raw WhatsApp)
- Operations alerts (triggered only on significant events)
- Immutable amendment audit trail
"""

import pytest

from src.models import (
    Business,
    Customer,
    MessageChannel,
    NotificationTrigger,
    OrderItem,
    OrderStatus,
)
from src.notification_service import NotificationService
from src.operations_dashboard import OperationsDashboard
from src.order_service import HIGH_VALUE_THRESHOLD, OrderService, OrderTransitionError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def customer():
    return Customer.create("Mercy", "+254700000001")


@pytest.fixture
def customer2():
    return Customer.create("Alice", "+254700000002")


@pytest.fixture
def business():
    return Business.create("Healthy Eats", "+254711111111")


@pytest.fixture
def business2():
    return Business.create("Fresh Market", "+254722222222")


@pytest.fixture
def basic_items():
    return [OrderItem("Chia Seeds", 2, 200.0)]


@pytest.fixture
def expensive_items():
    """Items whose total exceeds the high-value threshold."""
    return [OrderItem("Premium Bundle", 1, HIGH_VALUE_THRESHOLD)]


@pytest.fixture
def svc():
    return NotificationService()


@pytest.fixture
def dashboard(svc):
    return OperationsDashboard(svc.orders, svc.messages)


# ---------------------------------------------------------------------------
# Order model
# ---------------------------------------------------------------------------


class TestOrderModel:
    def test_order_id_format(self, customer, business, basic_items):
        from src.models import Order
        order = Order.create(customer, business, basic_items)
        assert order.order_id.startswith("ORD-")

    def test_order_total(self, customer, business):
        from src.models import Order
        items = [OrderItem("A", 2, 100.0), OrderItem("B", 3, 50.0)]
        order = Order.create(customer, business, items)
        assert order.total == 350.0

    def test_amendment_appended_to_audit_trail(self, customer, business, basic_items):
        from src.models import Order
        order = Order.create(customer, business, basic_items)
        order.add_amendment("Remove seeds")
        order.add_amendment("Add avocado")
        assert len(order.amendments) == 2
        assert order.amendments[0].description == "Remove seeds"


# ---------------------------------------------------------------------------
# Order service — state machine
# ---------------------------------------------------------------------------


class TestOrderService:
    def test_pending_to_processed(self, customer, business, basic_items):
        svc = OrderService()
        order = svc.create_order(customer, business, basic_items)
        svc.advance_order(order.order_id, OrderStatus.PROCESSED)
        assert svc.get_order(order.order_id).status == OrderStatus.PROCESSED

    def test_processed_to_delivered(self, customer, business, basic_items):
        svc = OrderService()
        order = svc.create_order(customer, business, basic_items)
        svc.advance_order(order.order_id, OrderStatus.PROCESSED)
        svc.advance_order(order.order_id, OrderStatus.DELIVERED)
        assert svc.get_order(order.order_id).status == OrderStatus.DELIVERED

    def test_invalid_transition_raises(self, customer, business, basic_items):
        svc = OrderService()
        order = svc.create_order(customer, business, basic_items)
        with pytest.raises(OrderTransitionError):
            svc.advance_order(order.order_id, OrderStatus.DELIVERED)

    def test_cannot_amend_processed_order(self, customer, business, basic_items):
        svc = OrderService()
        order = svc.create_order(customer, business, basic_items)
        svc.advance_order(order.order_id, OrderStatus.PROCESSED)
        with pytest.raises(OrderTransitionError):
            svc.amend_order(order.order_id, "Late change")

    def test_get_orders_for_business(self, customer, customer2,
                                      business, business2, basic_items):
        svc = OrderService()
        o1 = svc.create_order(customer, business, basic_items)
        o2 = svc.create_order(customer2, business, basic_items)
        svc.create_order(customer, business2, basic_items)
        orders = svc.get_orders_for_business(business.business_id)
        order_ids = {o.order_id for o in orders}
        assert o1.order_id in order_ids
        assert o2.order_id in order_ids
        assert len(orders) == 2

    def test_high_value_detection(self, customer, business, expensive_items):
        svc = OrderService()
        order = svc.create_order(customer, business, expensive_items)
        assert svc.is_high_value(order)

    def test_non_high_value_not_flagged(self, customer, business, basic_items):
        svc = OrderService()
        order = svc.create_order(customer, business, basic_items)
        assert not svc.is_high_value(order)


# ---------------------------------------------------------------------------
# Messaging service — thread isolation
# ---------------------------------------------------------------------------


class TestMessagingService:
    def test_one_thread_per_order(self, customer, business, basic_items, svc):
        order1 = svc.place_order(customer, business, basic_items)
        order2 = svc.place_order(customer, business, basic_items)
        thread1 = svc.messages.get_thread(order1.order_id)
        thread2 = svc.messages.get_thread(order2.order_id)
        assert thread1 is not None
        assert thread2 is not None
        assert thread1.thread_id != thread2.thread_id

    def test_customer_receives_confirmation(self, customer, business, basic_items, svc):
        order = svc.place_order(customer, business, basic_items)
        thread = svc.messages.get_thread(order.order_id)
        customer_msgs = [m for m in thread.messages if m.recipient == "customer"]
        assert len(customer_msgs) >= 1
        assert order.order_id in customer_msgs[0].body
        assert customer.name in customer_msgs[0].body

    def test_business_receives_dashboard_notification(self, customer, business,
                                                       basic_items, svc):
        order = svc.place_order(customer, business, basic_items)
        thread = svc.messages.get_thread(order.order_id)
        biz_msgs = [m for m in thread.messages if m.recipient == "business"]
        assert len(biz_msgs) == 1
        # Business notification uses IN_APP channel, not raw WhatsApp
        assert biz_msgs[0].channel == MessageChannel.IN_APP
        assert "[DASHBOARD]" in biz_msgs[0].body

    def test_amendment_acknowledgement_sent_to_customer(self, customer, business,
                                                          basic_items, svc):
        order = svc.place_order(customer, business, basic_items)
        svc.amend_order(order.order_id, "No spice please")
        thread = svc.messages.get_thread(order.order_id)
        ack_msgs = [
            m for m in thread.messages
            if m.recipient == "customer" and "amendment" in m.body.lower()
        ]
        assert len(ack_msgs) == 1
        assert "No spice please" in ack_msgs[0].body

    def test_customer_receives_status_updates(self, customer, business, basic_items, svc):
        order = svc.place_order(customer, business, basic_items)
        svc.advance_order(order.order_id, OrderStatus.PROCESSED)
        svc.advance_order(order.order_id, OrderStatus.DELIVERED)
        thread = svc.messages.get_thread(order.order_id)
        status_msgs = [
            m for m in thread.messages
            if m.recipient == "customer" and m.sender == "business"
            and "order" in m.body.lower() and m.body != thread.messages[0].body
        ]
        # At least PROCESSED and DELIVERED notifications
        assert len(status_msgs) >= 2


# ---------------------------------------------------------------------------
# Flood prevention — businesses don't get 500 WhatsApp threads
# ---------------------------------------------------------------------------


class TestFloodPrevention:
    def test_500_orders_produce_no_raw_whatsapp_to_business(self, business):
        """
        Simulate 500 customers placing orders.

        Businesses must NOT receive individual WhatsApp messages; all
        business notifications must go through the IN_APP channel.
        """
        svc = NotificationService()
        whatsapp_to_business = 0

        for i in range(500):
            cust = Customer.create(f"Customer {i}",
                                   f"+2547{i % 100_000_000:08d}")
            order = svc.place_order(cust, business, [OrderItem("Product", 1, 50.0)])
            thread = svc.messages.get_thread(order.order_id)
            for msg in thread.messages:
                if (msg.recipient == "business"
                        and msg.channel == MessageChannel.WHATSAPP):
                    whatsapp_to_business += 1

        assert whatsapp_to_business == 0, (
            f"Business received {whatsapp_to_business} raw WhatsApp messages "
            "— flood prevention failed."
        )


# ---------------------------------------------------------------------------
# Operations alerts (n:1:n)
# ---------------------------------------------------------------------------


class TestOperationsAlerts:
    def test_high_value_order_triggers_ops_alert(self, customer, business,
                                                   expensive_items, svc):
        order = svc.place_order(customer, business, expensive_items)
        thread = svc.messages.get_thread(order.order_id)
        ops_msgs = [m for m in thread.messages if m.recipient == "operations"]
        assert len(ops_msgs) == 1
        assert "High-value" in ops_msgs[0].body

    def test_normal_order_does_not_alert_operations(self, customer, business,
                                                     basic_items, svc):
        order = svc.place_order(customer, business, basic_items)
        thread = svc.messages.get_thread(order.order_id)
        ops_msgs = [m for m in thread.messages if m.recipient == "operations"]
        assert len(ops_msgs) == 0

    def test_manual_ops_alert_stock_out(self, customer, business, basic_items, svc):
        order = svc.place_order(customer, business, basic_items)
        svc.trigger_operations_alert(
            order.order_id,
            NotificationTrigger.STOCK_OUT,
            detail="Chia Seeds out of stock",
        )
        thread = svc.messages.get_thread(order.order_id)
        ops_msgs = [m for m in thread.messages if m.recipient == "operations"]
        assert len(ops_msgs) == 1
        assert "stock_out" in ops_msgs[0].body

    def test_manual_ops_alert_fraud(self, customer, business, basic_items, svc):
        order = svc.place_order(customer, business, basic_items)
        svc.trigger_operations_alert(
            order.order_id,
            NotificationTrigger.FRAUD_ANOMALY,
            detail="Unusual order pattern",
        )
        thread = svc.messages.get_thread(order.order_id)
        ops_msgs = [m for m in thread.messages if m.recipient == "operations"]
        assert any("fraud_anomaly" in m.body for m in ops_msgs)


# ---------------------------------------------------------------------------
# Operations dashboard (aggregated view)
# ---------------------------------------------------------------------------


class TestOperationsDashboard:
    def test_dashboard_aggregates_by_business(self, customer, customer2,
                                               business, business2,
                                               basic_items, svc, dashboard):
        svc.place_order(customer, business, basic_items)
        svc.place_order(customer2, business, basic_items)
        svc.place_order(customer, business2, basic_items)

        report = dashboard.generate_report()
        assert report.total_orders == 3

        b1_summary = next(
            s for s in report.summaries
            if s.business_id == business.business_id
        )
        assert b1_summary.total_orders == 2

    def test_dashboard_counts_statuses(self, customer, customer2,
                                        business, basic_items, svc, dashboard):
        o1 = svc.place_order(customer, business, basic_items)
        o2 = svc.place_order(customer2, business, basic_items)
        svc.advance_order(o1.order_id, OrderStatus.PROCESSED)

        report = dashboard.generate_report()
        b_summary = next(
            s for s in report.summaries
            if s.business_id == business.business_id
        )
        assert b_summary.pending == 1
        assert b_summary.processed == 1

    def test_dashboard_shows_ops_alerts(self, customer, business,
                                         expensive_items, svc, dashboard):
        svc.place_order(customer, business, expensive_items)
        report = dashboard.generate_report()
        assert len(report.operations_alerts) >= 1

    def test_dashboard_drill_into_thread(self, customer, business,
                                          basic_items, svc, dashboard):
        order = svc.place_order(customer, business, basic_items)
        thread = dashboard.get_order_thread(order.order_id)
        assert thread is not None
        assert thread.order_id == order.order_id

    def test_dashboard_shows_amendment_count(self, customer, business,
                                              basic_items, svc, dashboard):
        order = svc.place_order(customer, business, basic_items)
        svc.amend_order(order.order_id, "Change 1")
        svc.amend_order(order.order_id, "Change 2")

        report = dashboard.generate_report()
        b_summary = next(
            s for s in report.summaries
            if s.business_id == business.business_id
        )
        assert b_summary.amendments == 2


# ---------------------------------------------------------------------------
# Customer preferred channel
# ---------------------------------------------------------------------------


class TestPreferredChannel:
    def test_sms_customer_receives_sms(self, business, basic_items, svc):
        sms_customer = Customer.create(
            "Bob", "+254733333333", preferred_channel=MessageChannel.SMS
        )
        order = svc.place_order(sms_customer, business, basic_items)
        thread = svc.messages.get_thread(order.order_id)
        customer_msgs = [m for m in thread.messages if m.recipient == "customer"]
        assert all(m.channel == MessageChannel.SMS for m in customer_msgs)
