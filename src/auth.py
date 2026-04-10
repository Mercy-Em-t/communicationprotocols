from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .models import Order


class Role(Enum):
    CUSTOMER = "customer"
    BUSINESS = "business"
    OPERATIONS = "operations"
    SYSTEM = "system"


@dataclass(frozen=True)
class AuthContext:
    role: Role
    actor_id: str
    tenant_id: str


class AuthorizationError(PermissionError):
    """Raised when an actor is not allowed to perform an action."""


def require_same_tenant(ctx: Optional[AuthContext], tenant_id: str) -> None:
    if ctx is None:
        return
    if ctx.tenant_id != tenant_id:
        raise AuthorizationError("Cross-tenant access is not allowed.")


def authorize_order_read(ctx: Optional[AuthContext], order: Order) -> None:
    if ctx is None or ctx.role == Role.SYSTEM:
        return
    require_same_tenant(ctx, order.tenant_id)
    if ctx.role == Role.OPERATIONS:
        return
    if ctx.role == Role.CUSTOMER and ctx.actor_id == order.customer.customer_id:
        return
    if ctx.role == Role.BUSINESS and ctx.actor_id == order.business.business_id:
        return
    raise AuthorizationError("Not allowed to access this order.")


def authorize_order_action(ctx: Optional[AuthContext], order: Order,
                           allowed_roles: Tuple[Role, ...]) -> None:
    if ctx is None:
        return
    require_same_tenant(ctx, order.tenant_id)
    if ctx.role not in allowed_roles:
        raise AuthorizationError("Role not allowed for this action.")
    if ctx.role == Role.CUSTOMER and ctx.actor_id != order.customer.customer_id:
        raise AuthorizationError("Customer can only act on own orders.")
    if ctx.role == Role.BUSINESS and ctx.actor_id != order.business.business_id:
        raise AuthorizationError("Business can only act on own orders.")
