from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Dict, Tuple

from .models import Order


class InventoryError(Exception):
    """Raised for inventory stock failures."""


@dataclass
class Reservation:
    order_id: str
    tenant_id: str
    reserved: Dict[str, int]


class InventoryService:
    """In-memory inventory with atomic reserve/release operations."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stock: Dict[Tuple[str, str], int] = {}
        self._reservations: Dict[str, Reservation] = {}

    def set_stock(self, tenant_id: str, product_name: str, quantity: int) -> None:
        with self._lock:
            self._stock[(tenant_id, product_name)] = quantity

    def get_stock(self, tenant_id: str, product_name: str) -> int:
        return self._stock.get((tenant_id, product_name), 0)

    def reserve_for_order(self, order: Order) -> None:
        with self._lock:
            required: Dict[str, int] = {}
            for item in order.items:
                required[item.product_name] = required.get(item.product_name, 0) + item.quantity

            for product_name, qty in required.items():
                key = (order.tenant_id, product_name)
                if key not in self._stock:
                    continue
                if self._stock.get(key, 0) < qty:
                    raise InventoryError(f"Insufficient stock for {product_name}.")

            for product_name, qty in required.items():
                key = (order.tenant_id, product_name)
                if key not in self._stock:
                    continue
                self._stock[key] = self._stock[key] - qty

            self._reservations[order.order_id] = Reservation(
                order_id=order.order_id,
                tenant_id=order.tenant_id,
                reserved=required,
            )

    def release_for_order(self, order_id: str) -> None:
        with self._lock:
            reservation = self._reservations.pop(order_id, None)
            if reservation is None:
                return
            for product_name, qty in reservation.reserved.items():
                key = (reservation.tenant_id, product_name)
                self._stock[key] = self._stock.get(key, 0) + qty
