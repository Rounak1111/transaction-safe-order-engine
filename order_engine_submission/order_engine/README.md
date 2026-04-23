# 🚀 Transaction-Safe Order Engine

> **Hiring Assignment Submission — SARAL ERP Solutions Pvt. Ltd.**  
> A fault-tolerant, production-correct order processing system built with **FastAPI · SQLAlchemy · SQLite**

---

## 📁 Project Structure

```
order_engine/
├── app/
│   ├── __init__.py
│   ├── main.py           →  FastAPI app + all endpoints
│   ├── models.py         →  SQLAlchemy ORM (6 tables)
│   ├── schemas.py        →  Pydantic request / response models
│   ├── database.py       →  DB engine, session factory, get_db dependency
│   └── order_service.py  →  All business logic (transactions, rollback, idempotency)
├── requirements.txt
└── README.md
```

---

## ⚡ How to Run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the server
uvicorn app.main:app --reload

# 3. Seed demo data (run once after first start)
curl http://localhost:8000/seed

# 4. Open interactive Swagger UI
http://localhost:8000/docs
```

Seeding creates:
| Resource | Details |
|---|---|
| Users | Alice (id=1), Bob (id=2), Charlie (id=3) |
| Products | Laptop (id=1), Mouse (id=2), Keyboard (id=3) |
| Inventory | Laptop→5, Mouse→**1** (great for race-condition demo), Keyboard→10 |

---

## 🔌 API Reference

### `POST /orders` — Place an order (idempotent)

**Request body:**
```json
{
  "user_id": 1,
  "items": [
    { "product_id": 1, "quantity": 2 },
    { "product_id": 3, "quantity": 1 }
  ],
  "idempotency_key": "my-unique-request-id-001"
}
```

**Response (success):**
```json
{
  "id": 1,
  "user_id": 1,
  "status": "SUCCESS",
  "items": [
    { "product_id": 1, "quantity": 2 },
    { "product_id": 3, "quantity": 1 }
  ],
  "created_at": "2024-01-15T10:30:00",
  "updated_at": "2024-01-15T10:30:01"
}
```

### `GET /orders/{id}` — Fetch order by ID

Returns the order with its current status: `INIT` → `PROCESSING` → `SUCCESS` / `FAILED` / `ROLLED_BACK`

### `GET /seed` — Seed demo data

Populates the database with users, products, and inventory for testing.

### `GET /health` — Health check

---

## 🗃️ Database Schema

```
users            → id, name
products         → id, name
inventory        → id, product_id (FK), stock
orders           → id, user_id (FK), status, created_at, updated_at
order_items      → id, order_id (FK), product_id (FK), quantity
idempotency_keys → id, key (UNIQUE), response (JSON), created_at
```

---

## 🧠 Design Explanation

### 1. Transaction Handling (DB-level)

Every order flows inside a **single SQLAlchemy session/transaction**.

```
BEGIN TRANSACTION
  ├── INSERT order           (status = INIT)
  ├── INSERT order_items
  ├── UPDATE order status    → PROCESSING
  ├── UPDATE inventory       (atomic guard per item)
  ├── simulate payment
  ├── UPDATE order status    → SUCCESS  or  ROLLED_BACK
  └── INSERT idempotency_key
COMMIT  ←  only if every step above succeeded
```

`db.flush()` stages each SQL write without committing, so the order gets an ID that can be referenced by items — but nothing is permanent until the final `db.commit()`. Any unhandled exception causes the session to roll back via the `get_db` dependency's `finally` block.

---

### 2. Inventory Locking Strategy (Optimistic / SQLite-safe)

SQLite does not support `SELECT … FOR UPDATE` (pessimistic locking).  
Instead, we use a **single atomic UPDATE with a WHERE guard** — a form of optimistic locking:

```sql
UPDATE inventory
SET    stock = stock - :qty
WHERE  product_id = :pid
  AND  stock >= :qty        ← the guard
```

- If stock is sufficient → `rowcount = 1` → deduction succeeds.  
- If stock is zero or insufficient → `rowcount = 0` → we raise `ValueError` immediately.  
- Because SQLite serialises all writes at the file level, two concurrent threads cannot both pass this guard for the same product — one will always see `rowcount = 0`.

On **PostgreSQL** this would be replaced with `SELECT … FOR UPDATE` row-level locking for even stronger guarantees.

---

### 3. Failure & Rollback Mechanism

There are two explicit rollback paths in `order_service.py`:

**Path A — Out of stock (partial failure)**  
The service loops through order items one by one. If item N fails the stock guard, items 0..N-1 were already deducted. `_restore_inventory()` issues compensating `UPDATE inventory SET stock = stock + qty` for those items. The order is marked `FAILED` and the compensating state is committed.

**Path B — Payment failure**  
After all inventory is deducted, payment is simulated (~70% success rate). On failure, `_restore_inventory()` restores all deducted quantities and the order is marked `ROLLED_BACK`. Both the restored stock and the final status are committed atomically.

```
Payment FAILED?
  └── _restore_inventory(all deducted items)
  └── order.status = ROLLED_BACK
  └── db.commit()   ← inventory is back, order is closed
```

---

### 4. Idempotency Implementation

Before any DB writes, the service checks the `idempotency_keys` table:

```python
existing = db.query(IdempotencyKey).filter_by(key=req.idempotency_key).first()
if existing:
    return json.loads(existing.response)   # return cached result, stop here
```

- **Key found** → return the stored JSON response immediately. No order created, no inventory touched. Safe for unlimited retries.  
- **Key not found** → process normally, then persist `(key, serialised_response)` in the same commit as the order.

This guarantees that a client retrying after a network timeout always gets the exact same answer and never causes double-deduction or duplicate orders.

---

### 5. Concurrency Handling

Two users ordering the last available item simultaneously is handled by the atomic UPDATE guard described in section 2. One thread will commit the deduction first; the other will find `stock >= qty` is false and receive a `FAILED` order with an "Insufficient stock" message.

---

## 🧪 Edge Case Coverage

| Scenario | What Happens |
|---|---|
| **Same request sent twice** | Second call returns the cached response; no duplicate order or deduction |
| **Two users buying the last item** | Exactly one gets `SUCCESS`; the other gets `FAILED` (Insufficient stock) |
| **Payment failure** | Inventory fully restored; order marked `ROLLED_BACK` |
| **Item out of stock** | Order marked `FAILED`; no inventory changes committed |
| **Partial failure (item 2 of 3 out of stock)** | Items already deducted are restored; order marked `FAILED` |

---

## 🏃 Concurrency Demo

Open two terminals and fire both requests at the same time (Mouse has stock = 1):

```bash
# Terminal 1
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id":1,"items":[{"product_id":2,"quantity":1}],"idempotency_key":"race-user1"}'

# Terminal 2 — run at the same time
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id":2,"items":[{"product_id":2,"quantity":1}],"idempotency_key":"race-user2"}'
```

**Expected:** One response shows `"status": "SUCCESS"`, the other shows `"status": "FAILED"`.

---

## 🔁 Idempotency Demo

```bash
# Send the exact same request twice
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id":1,"items":[{"product_id":1,"quantity":1}],"idempotency_key":"demo-key-001"}'

curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id":1,"items":[{"product_id":1,"quantity":1}],"idempotency_key":"demo-key-001"}'
```

**Expected:** Both responses are identical. Stock is deducted only once.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| API Framework | FastAPI 0.115 |
| ORM | SQLAlchemy 2.0 |
| Database | SQLite (file-based, zero setup) |
| Validation | Pydantic v2 |
| Server | Uvicorn |

---

## 📋 Requirements Checklist

- ✅ User can place an order with multiple products and quantities  
- ✅ Inventory updated atomically  
- ✅ Payment simulation (SUCCESS → confirmed, FAILURE → rollback)  
- ✅ Order states: `INIT → PROCESSING → SUCCESS / FAILED / ROLLED_BACK`  
- ✅ Partial failure handling (mid-loop out-of-stock)  
- ✅ Idempotency key — prevents duplicate execution  
- ✅ Retry safety — no double deduction or duplicate orders  
- ✅ Concurrency safety — two users cannot both buy the last item  
- ✅ RESTful API design with Swagger UI  
- ✅ Clean architecture (Service layer pattern)  

---

*Submitted for SARAL ERP Solutions Pvt. Ltd. — Hiring Assignment*
