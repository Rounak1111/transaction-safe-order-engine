"""
SQLAlchemy ORM models for all database tables.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Enum, ForeignKey,
    DateTime, Text, UniqueConstraint
)
from sqlalchemy.orm import relationship

from app.database import Base


class OrderStatus(str, enum.Enum):
    INIT        = "INIT"
    PROCESSING  = "PROCESSING"
    SUCCESS     = "SUCCESS"
    FAILED      = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class User(Base):
    __tablename__ = "users"

    id   = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    orders = relationship("Order", back_populates="user")


class Product(Base):
    __tablename__ = "products"

    id   = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    inventory = relationship("Inventory", back_populates="product", uselist=False)


class Inventory(Base):
    __tablename__ = "inventory"

    id         = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), unique=True, nullable=False)
    stock      = Column(Integer, nullable=False, default=0)

    product = relationship("Product", back_populates="inventory")


class Order(Base):
    __tablename__ = "orders"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    status     = Column(Enum(OrderStatus), default=OrderStatus.INIT, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user  = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id         = Column(Integer, primary_key=True, index=True)
    order_id   = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity   = Column(Integer, nullable=False)

    order = relationship("Order", back_populates="items")


class IdempotencyKey(Base):
    """
    Stores previously seen idempotency keys and their saved responses.
    If the same key arrives again, we return the cached response immediately.
    """
    __tablename__ = "idempotency_keys"

    id           = Column(Integer, primary_key=True, index=True)
    key          = Column(String, unique=True, nullable=False, index=True)
    response     = Column(Text, nullable=False)   # JSON-serialised response
    created_at   = Column(DateTime, default=datetime.utcnow)
