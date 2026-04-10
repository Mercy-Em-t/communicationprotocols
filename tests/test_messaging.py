"""Tests for strict lifecycle + WhatsApp orchestration flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.auth import AuthContext, AuthorizationError, Role
from src.inventory_service import InventoryError, InventoryService
from src.messaging_service import MessagingService
from src.models import (
    Business,
    DeliveryStatus,
    MessageChannel,
    NotificationTrigger,
    Customer,
    OrderItem,
    OrderStatus,
)
from src.notification_service import NotificationService
from src.operations_dashboard import OperationsDashboard
from src.order_service import OrderService, OrderTransitionError


@pytest.fixture
def customer():
    return Customer.create("Mercy", "+254700000001", tenant_id="t1")


@pytest.fixture
def customer2():
    return Customer.create("Alice", "+254700000002", tenant_id="t1")


@pytest.fixture
def business():
    return Business.create("Healthy Eats", "+254711111111", tenant_id="t1")


@pytest.fixture
def business2():
    return Business.create("Fresh Market", "+254722222222", tenant_id="t1")


@pytest.fixture
def basic_items():
    return [OrderItem("Chia Seeds", 2, 200.0)]


@pytest.fixture
def svc():
    return NotificationService()


@pytest.fixture
def dashboard(svc):
    return OperationsDashboard(svc.orders, svc.messages)


class TestLifecycleFlow:
    def test_checkout_starts_shop_confirmation_and_messages(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)

        assert order.status == OrderStatus.PENDING_SHOP_CONFIRMATION
        thread = svc.messages.get_thread(order.order_id)
        assert thread is not None

        customer_msgs = [m for m in thread.messages if m.recipient == "customer"]
        business_msgs = [m for m in thread.messages if m.recipient == "business"]

        assert any("received" in m.body.lower() for m in customer_msgs)
        assert any(m.channel == MessageChannel.IN_APP for m in business_msgs)
        assert any(m.channel == MessageChannel.WHATSAPP for m in business_msgs)

    def test_full_delivery_lifecycle(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)

        shop_actor = AuthContext(Role.BUSINESS, business.business_id, "t1")
        customer_actor = AuthContext(Role.CUSTOMER, customer.customer_id, "t1")
        system_actor = AuthContext(Role.SYSTEM, "system", "t1")

        order = svc.shop_accept_order(order.order_id, actor=shop_actor)
        assert order.status == OrderStatus.AWAITING_CUSTOMER_CONFIRMATION

        order = svc.customer_confirm_items(order.order_id, actor=customer_actor)
        assert order.status == OrderStatus.AWAITING_PAYMENT

        order = svc.start_payment(order.order_id, actor=customer_actor)
        assert order.status == OrderStatus.PAYMENT_PROCESSING

        order = svc.confirm_payment_webhook(order.order_id, "pay-1", True, actor=system_actor)
        assert order.status == OrderStatus.PAID

        order = svc.start_fulfillment(order.order_id, actor=shop_actor)
        assert order.status == OrderStatus.FULFILLING

        order = svc.mark_out_for_delivery(order.order_id, actor=shop_actor)
        assert order.status == OrderStatus.OUT_FOR_DELIVERY

        order = svc.complete_order(order.order_id, actor=shop_actor)
        assert order.status == OrderStatus.COMPLETED

    def test_shop_reject_path(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)
        actor = AuthContext(Role.BUSINESS, business.business_id, "t1")

        order = svc.shop_reject_order(order.order_id, actor=actor)
        assert order.status == OrderStatus.REJECTED_BY_SHOP

        thread = svc.messages.get_thread(order.order_id)
        assert any("rejected" in m.body.lower() for m in thread.messages if m.recipient == "customer")

    def test_invalid_transition_blocked(self, customer, business, basic_items):
        orders = OrderService()
        order = orders.create_order(customer, business, basic_items)

        with pytest.raises(OrderTransitionError):
            orders.advance_order(order.order_id, OrderStatus.PAID)


class TestWhatsAppInbound:
    def test_business_accept_reply_mapping(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)

        action = svc.handle_incoming_whatsapp(
            from_phone=business.phone,
            order_id=order.order_id,
            message="1",
            message_id="m-1",
            timestamp=datetime.now(tz=timezone.utc),
        )

        assert action == "SHOP_ACCEPT_ORDER"
        assert svc.orders.get_order(order.order_id).status == OrderStatus.AWAITING_CUSTOMER_CONFIRMATION

    def test_customer_confirm_reply_mapping(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)
        svc.shop_accept_order(order.order_id, actor=AuthContext(Role.BUSINESS, business.business_id, "t1"))

        action = svc.handle_incoming_whatsapp(
            from_phone=customer.phone,
            order_id=order.order_id,
            message="1",
            message_id="m-2",
            timestamp=datetime.now(tz=timezone.utc),
        )

        assert action == "CUSTOMER_CONFIRM_ITEMS"
        assert svc.orders.get_order(order.order_id).status == OrderStatus.AWAITING_PAYMENT

    def test_replay_message_ignored(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)
        first = svc.handle_incoming_whatsapp(
            from_phone=business.phone,
            order_id=order.order_id,
            message="2",
            message_id="dup-1",
            timestamp=datetime.now(tz=timezone.utc),
        )
        second = svc.handle_incoming_whatsapp(
            from_phone=business.phone,
            order_id=order.order_id,
            message="2",
            message_id="dup-1",
            timestamp=datetime.now(tz=timezone.utc),
        )

        assert first == "SHOP_REJECT_ORDER"
        assert second == "duplicate_ignored"

    def test_invalid_webhook_signature_rejected(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)
        with pytest.raises(PermissionError):
            svc.handle_incoming_whatsapp(
                from_phone=business.phone,
                order_id=order.order_id,
                message="1",
                message_id="m-3",
                timestamp=datetime.now(tz=timezone.utc),
                signature_valid=False,
            )

    def test_old_timestamp_rejected(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)
        with pytest.raises(PermissionError):
            svc.handle_incoming_whatsapp(
                from_phone=business.phone,
                order_id=order.order_id,
                message="1",
                message_id="m-4",
                timestamp=datetime.now(tz=timezone.utc) - timedelta(minutes=10),
            )


class TestPayments:
    def test_payment_webhook_idempotency(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)
        shop_actor = AuthContext(Role.BUSINESS, business.business_id, "t1")
        customer_actor = AuthContext(Role.CUSTOMER, customer.customer_id, "t1")
        system_actor = AuthContext(Role.SYSTEM, "system", "t1")

        svc.shop_accept_order(order.order_id, actor=shop_actor)
        svc.customer_confirm_items(order.order_id, actor=customer_actor)
        svc.start_payment(order.order_id, actor=customer_actor)

        paid = svc.confirm_payment_webhook(order.order_id, "pay-ref-1", True, actor=system_actor)
        again = svc.confirm_payment_webhook(order.order_id, "pay-ref-1", True, actor=system_actor)

        assert paid.status == OrderStatus.PAID
        assert again.status == OrderStatus.PAID

    def test_payment_reference_cannot_be_reused_cross_order(self, svc, customer, customer2, business, basic_items):
        shop_actor = AuthContext(Role.BUSINESS, business.business_id, "t1")
        system_actor = AuthContext(Role.SYSTEM, "system", "t1")

        c1_actor = AuthContext(Role.CUSTOMER, customer.customer_id, "t1")
        c2_actor = AuthContext(Role.CUSTOMER, customer2.customer_id, "t1")

        o1 = svc.place_order(customer, business, basic_items)
        o2 = svc.place_order(customer2, business, basic_items)

        svc.shop_accept_order(o1.order_id, actor=shop_actor)
        svc.customer_confirm_items(o1.order_id, actor=c1_actor)
        svc.start_payment(o1.order_id, actor=c1_actor)
        svc.confirm_payment_webhook(o1.order_id, "shared-ref", True, actor=system_actor)

        svc.shop_accept_order(o2.order_id, actor=shop_actor)
        svc.customer_confirm_items(o2.order_id, actor=c2_actor)
        svc.start_payment(o2.order_id, actor=c2_actor)

        with pytest.raises(ValueError):
            svc.confirm_payment_webhook(o2.order_id, "shared-ref", True, actor=system_actor)


class TestReliability:
    def test_failed_channel_retries_then_dead_letters(self, customer, business, basic_items):
        messaging = MessagingService(fail_channels={MessageChannel.WHATSAPP}, max_delivery_attempts=2)
        svc = NotificationService(messaging_service=messaging)

        order = svc.place_order(customer, business, basic_items)
        thread = svc.messages.get_thread(order.order_id)
        customer_msg = next(m for m in thread.messages if m.recipient == "customer")

        svc.messages._dispatch_outbox(order.order_id)

        assert customer_msg.delivery_attempts == 2
        assert customer_msg.delivery_status == DeliveryStatus.DEAD_LETTER
        assert len(svc.messages.get_dead_letters()) >= 1

    def test_delivery_status_callback_updates_message(self, svc, customer, business, basic_items):
        order = svc.place_order(customer, business, basic_items)
        thread = svc.messages.get_thread(order.order_id)
        msg = next(m for m in thread.messages if m.recipient == "customer")

        # simulate provider callback failure
        svc.process_delivery_status_callback(msg.message_id, delivered=False, error="provider_down")

        updated = svc.messages.get_message_by_id(msg.message_id)
        assert updated is not None
        assert updated.delivery_status in {DeliveryStatus.FAILED, DeliveryStatus.DEAD_LETTER}
        assert updated.last_error == "provider_down"


class TestReportingAndDashboard:
    def test_lifecycle_events_are_recorded(self, svc, customer, business, basic_items):
        shop_actor = AuthContext(Role.BUSINESS, business.business_id, "t1")
        customer_actor = AuthContext(Role.CUSTOMER, customer.customer_id, "t1")
        system_actor = AuthContext(Role.SYSTEM, "system", "t1")

        order = svc.place_order(customer, business, basic_items)
        svc.shop_accept_order(order.order_id, actor=shop_actor)
        svc.customer_confirm_items(order.order_id, actor=customer_actor)
        svc.start_payment(order.order_id, actor=customer_actor)
        svc.confirm_payment_webhook(order.order_id, "ev-ref", True, actor=system_actor)

        events = svc.list_order_events(order_id=order.order_id)
        types = {e.event_type for e in events}

        assert "ORDER_CREATED" in types
        assert "ORDER_SUBMITTED_TO_SHOP" in types
        assert "ORDER_ACCEPTED_BY_SHOP" in types
        assert "ORDER_AWAITING_PAYMENT" in types
        assert "ORDER_PAID" in types

    def test_dashboard_metrics_include_rates(self, svc, dashboard, customer, customer2, business, basic_items):
        shop_actor = AuthContext(Role.BUSINESS, business.business_id, "t1")
        system_actor = AuthContext(Role.SYSTEM, "system", "t1")

        c1 = AuthContext(Role.CUSTOMER, customer.customer_id, "t1")
        c2 = AuthContext(Role.CUSTOMER, customer2.customer_id, "t1")

        o1 = svc.place_order(customer, business, basic_items)
        o2 = svc.place_order(customer2, business, basic_items)

        svc.shop_accept_order(o1.order_id, actor=shop_actor)
        svc.customer_confirm_items(o1.order_id, actor=c1)
        svc.start_payment(o1.order_id, actor=c1)
        svc.confirm_payment_webhook(o1.order_id, "dash-pay", True, actor=system_actor)

        svc.shop_reject_order(o2.order_id, actor=shop_actor)

        report = dashboard.generate_report()
        summary = next(s for s in report.summaries if s.business_id == business.business_id)

        assert summary.total_orders == 2
        assert summary.payment_completion_rate > 0
        assert summary.rejection_rate > 0
        assert isinstance(report.conversion_by_state, dict)

    def test_sla_breach_visible_in_dashboard(self, customer, business, basic_items):
        svc = NotificationService(sla_minutes=0)
        dashboard = OperationsDashboard(svc.orders, svc.messages)
        order = svc.place_order(customer, business, basic_items)

        breached = svc.check_sla_breaches()
        report = dashboard.generate_report()

        assert order.order_id in breached
        assert order.order_id in report.sla_breached_orders


class TestSecurityAndIsolation:
    def test_cross_tenant_order_creation_rejected(self, basic_items):
        customer = Customer.create("Mercy", "+254700000001", tenant_id="t1")
        business = Business.create("Healthy Eats", "+254711111111", tenant_id="t2")
        svc = NotificationService()
        with pytest.raises(ValueError):
            svc.place_order(customer, business, basic_items)

    def test_business_cannot_accept_other_business_order(self, svc, customer, business, business2, basic_items):
        order = svc.place_order(customer, business, basic_items)
        wrong_actor = AuthContext(Role.BUSINESS, business2.business_id, "t1")
        with pytest.raises(AuthorizationError):
            svc.shop_accept_order(order.order_id, actor=wrong_actor)


class TestInventory:
    def test_insufficient_stock_blocks_order(self, customer, business):
        inventory = InventoryService()
        inventory.set_stock(customer.tenant_id, "Chia Seeds", 1)
        svc = NotificationService(order_service=OrderService(inventory_service=inventory))

        with pytest.raises(InventoryError):
            svc.place_order(customer, business, [OrderItem("Chia Seeds", 2, 200.0)])

    def test_rejected_order_releases_stock(self, customer, business):
        inventory = InventoryService()
        inventory.set_stock(customer.tenant_id, "Chia Seeds", 2)
        svc = NotificationService(order_service=OrderService(inventory_service=inventory))

        order = svc.place_order(customer, business, [OrderItem("Chia Seeds", 2, 200.0)])
        assert inventory.get_stock(customer.tenant_id, "Chia Seeds") == 0

        svc.shop_reject_order(order.order_id, actor=AuthContext(Role.BUSINESS, business.business_id, customer.tenant_id))
        assert inventory.get_stock(customer.tenant_id, "Chia Seeds") == 2
