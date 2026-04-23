"""
Pydantic schemas for request validation and response serialisation.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models import OrderStatus


# ── Request bodies ────────────────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    product_id: int
    quantity:   int = Field(..., gt=0)


class CreateOrderRequest(BaseModel):
    user_id:         int
    items:           List[OrderItemIn] = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)


# ── Response bodies ───────────────────────────────────────────────────────────

class OrderItemOut(BaseModel):
    product_id: int
    quantity:   int

    class Config:
        from_attributes = True


class OrderResponse(BaseModel):
    id:         int
    user_id:    int
    status:     OrderStatus
    items:      List[OrderItemOut]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
