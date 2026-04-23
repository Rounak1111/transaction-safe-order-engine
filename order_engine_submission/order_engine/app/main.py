"""
Transaction-Safe Order Engine — FastAPI entrypoint.

Endpoints
─────────
POST /orders         place a new order (idempotent)
GET  /orders/{id}    fetch order status
GET  /seed           seed demo data (users, products, inventory)
"""

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models   import User, Product, Inventory, OrderStatus
from app.schemas  import CreateOrderRequest, OrderResponse
from app import order_service

# Create all tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Transaction-Safe Order Engine",
    description="Demonstrates DB transactions, rollback, idempotency, and concurrency safety.",
    version="1.0.0",
)


# ─────────────────────────────────────────────────────────────────────────────
# Seed endpoint (convenience — run once to populate demo data)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/seed", summary="Seed demo users, products, and inventory")
def seed(db: Session = Depends(get_db)):
    # Idempotent — skip if data already exists
    if db.query(User).count() > 0:
        return {"message": "Already seeded"}

    users = [User(name="Alice"), User(name="Bob"), User(name="Charlie")]
    db.add_all(users)
    db.flush()

    products = [
        Product(name="Laptop"),
        Product(name="Mouse"),
        Product(name="Keyboard"),
    ]
    db.add_all(products)
    db.flush()

    inventory = [
        Inventory(product_id=products[0].id, stock=5),
        Inventory(product_id=products[1].id, stock=1),   # ← only 1 left (race demo)
        Inventory(product_id=products[2].id, stock=10),
    ]
    db.add_all(inventory)
    db.commit()

    return {
        "message": "Seeded successfully",
        "users":    [{"id": u.id, "name": u.name} for u in users],
        "products": [
            {"id": p.id, "name": p.name,
             "stock": next(i.stock for i in inventory if i.product_id == p.id)}
            for p in products
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Order endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/orders", response_model=None, status_code=201,
          summary="Place a new order (idempotent)")
def place_order(req: CreateOrderRequest, db: Session = Depends(get_db)):
    """
    Places an order for the given user and list of items.

    - **idempotency_key**: send the same key twice → identical response, no
      double-processing.
    - Stock is deducted atomically; if a product has insufficient stock the
      order is marked **FAILED** and inventory is not changed.
    - Payment is simulated (~70 % success).  On failure, inventory is restored
      and the order is marked **ROLLED_BACK**.
    """
    try:
        result = order_service.create_order(db, req)
        return result
    except ValueError as exc:
        # Raised by the service for out-of-stock; DB already rolled back.
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        db.rollback()   # safety net for unexpected errors
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")


@app.get("/orders/{order_id}", response_model=None,
         summary="Get order status by ID")
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Returns the current status and items of an order."""
    order = order_service.get_order(db, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "id":         order.id,
        "user_id":    order.user_id,
        "status":     order.status.value,
        "items":      [{"product_id": i.product_id, "quantity": i.quantity}
                       for i in order.items],
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", summary="Health check")
def health():
    return {"status": "ok"}
