"""
Order service — all business logic lives here.

Key design decisions
─────────────────────
1. TRANSACTIONS
   Every order flows inside a single SQLAlchemy transaction.
   If anything raises an exception the session is rolled back automatically
   by the caller (FastAPI dependency + try/except in the endpoint).

2. CONCURRENCY / LOCKING
   SQLite does not support SELECT … FOR UPDATE.  Instead we use an
   UPDATE … WHERE stock >= qty statement whose WHERE clause is the guard.
   If the row was NOT updated (rowcount == 0) the stock was insufficient,
   and we raise immediately.  This is a single atomic write that cannot
   race with another thread doing the same thing.

3. IDEMPOTENCY
   Before touching anything we look up the idempotency_key in the DB.
   If it already exists we return the cached JSON response and stop.
   After a successful (or explicitly failed) order we persist the key
   with the serialised response so future duplicate requests get the
   same answer.

4. ROLLBACK
   If payment simulation fails we manually restore inventory with an
   UPDATE and set the order status to ROLLED_BACK, then commit that
   compensating transaction.  The DB itself is always consistent.
"""

import json
import random
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    Inventory, Order, OrderItem, OrderStatus, IdempotencyKey
)
from app.schemas import CreateOrderRequest, OrderResponse


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_payment() -> bool:
    """Returns True ~70 % of the time (success), False otherwise (failure)."""
    return random.random() < 0.7


def _serialise_order(order: Order) -> str:
    """Turn an Order ORM object into a JSON string for idempotency storage."""
    data = {
        "id":         order.id,
        "user_id":    order.user_id,
        "status":     order.status.value,
        "items":      [{"product_id": i.product_id, "quantity": i.quantity}
                       for i in order.items],
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }
    return json.dumps(data)


def _order_from_json(raw: str) -> dict:
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Public service functions
# ─────────────────────────────────────────────────────────────────────────────

def get_order(db: Session, order_id: int) -> Order | None:
    return db.query(Order).filter(Order.id == order_id).first()


def create_order(db: Session, req: CreateOrderRequest) -> dict:
    """
    Full order-processing pipeline.

    Returns a plain dict that matches OrderResponse so it can be returned
    directly OR cached as JSON.
    """

    # ── 1. Idempotency check ──────────────────────────────────────────────────
    existing = (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.key == req.idempotency_key)
        .first()
    )
    if existing:
        # Same request seen before → return the original response unchanged.
        return _order_from_json(existing.response)

    # ── 2. Create order (status = INIT) ───────────────────────────────────────
    order = Order(user_id=req.user_id, status=OrderStatus.INIT,
                  created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(order)
    db.flush()  # get order.id without committing

    # Attach line items
    for item in req.items:
        db.add(OrderItem(order_id=order.id,
                         product_id=item.product_id,
                         quantity=item.quantity))
    db.flush()

    # ── 3. Transition → PROCESSING ────────────────────────────────────────────
    order.status     = OrderStatus.PROCESSING
    order.updated_at = datetime.utcnow()
    db.flush()

    # ── 4. Deduct inventory (concurrency-safe) ────────────────────────────────
    #
    # We run one UPDATE per line-item.  The WHERE clause (stock >= qty)
    # is the atomic guard — if two threads race, only one will match the
    # row; the loser gets rowcount == 0 and raises InsufficientStockError.
    #
    deducted: list[tuple[int, int]] = []   # (product_id, qty) already deducted

    try:
        for item in req.items:
            result = db.execute(
                text(
                    "UPDATE inventory "
                    "SET stock = stock - :qty "
                    "WHERE product_id = :pid AND stock >= :qty"
                ),
                {"qty": item.quantity, "pid": item.product_id},
            )

            if result.rowcount == 0:
                # Either out of stock OR product doesn't exist in inventory.
                raise ValueError(
                    f"Insufficient stock for product_id={item.product_id}"
                )

            deducted.append((item.product_id, item.quantity))

    except ValueError as exc:
        # Rollback inventory already deducted in this loop, mark order FAILED.
        _restore_inventory(db, deducted)
        order.status     = OrderStatus.FAILED
        order.updated_at = datetime.utcnow()
        db.flush()
        _save_idempotency_key(db, req.idempotency_key, order)
        db.commit()
        raise exc

    # ── 5. Simulate payment ───────────────────────────────────────────────────
    payment_ok = _simulate_payment()

    if payment_ok:
        order.status = OrderStatus.SUCCESS
    else:
        # Payment failed → restore inventory and mark ROLLED_BACK
        _restore_inventory(db, deducted)
        order.status = OrderStatus.ROLLED_BACK

    order.updated_at = datetime.utcnow()
    db.flush()

    # ── 6. Persist idempotency key + commit ───────────────────────────────────
    _save_idempotency_key(db, req.idempotency_key, order)
    db.commit()

    db.refresh(order)
    return _order_from_json(_serialise_order(order))


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _restore_inventory(db: Session, deducted: list[tuple[int, int]]) -> None:
    """Add back quantities that were already deducted (compensating writes)."""
    for product_id, qty in deducted:
        db.execute(
            text("UPDATE inventory SET stock = stock + :qty WHERE product_id = :pid"),
            {"qty": qty, "pid": product_id},
        )


def _save_idempotency_key(db: Session, key: str, order: Order) -> None:
    """Persist idempotency key → serialised order response."""
    db.add(IdempotencyKey(key=key, response=_serialise_order(order)))
