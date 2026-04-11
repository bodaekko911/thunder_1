from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text, Boolean, Date, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class B2BClient(Base):
    __tablename__ = "b2b_clients"

    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(200), nullable=False)
    contact_person  = Column(String(150))
    phone           = Column(String(50))
    email           = Column(String(150))
    address         = Column(String(300))
    payment_terms   = Column(String(50), default="immediate")  # immediate | net15 | net30 | net60 | consignment
    discount_pct    = Column(Numeric(6, 2), default=0)   # client-specific discount percentage
    credit_limit    = Column(Numeric(14,2), default=0)
    outstanding     = Column(Numeric(14,2), default=0)
    notes           = Column(Text)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    invoices        = relationship("B2BInvoice", back_populates="client")
    consignments    = relationship("Consignment", back_populates="client")


class B2BInvoice(Base):
    __tablename__ = "b2b_invoices"

    id              = Column(Integer, primary_key=True, index=True)
    invoice_number  = Column(String(30), unique=True, index=True)
    client_id       = Column(Integer, ForeignKey("b2b_clients.id"), nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    invoice_type    = Column(String(20), nullable=False)  # full_payment | credit | consignment
    status          = Column(String(20), default="unpaid")  # unpaid | paid | partial | consignment
    payment_method  = Column(String(30))  # cash | transfer | —
    subtotal        = Column(Numeric(14,2), default=0)
    discount        = Column(Numeric(14,2), default=0)
    total           = Column(Numeric(14,2), default=0)
    amount_paid     = Column(Numeric(14,2), default=0)
    due_date        = Column(Date, nullable=True)
    notes           = Column(Text)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    client          = relationship("B2BClient", back_populates="invoices")
    user            = relationship("User")
    items           = relationship("B2BInvoiceItem", back_populates="invoice", cascade="all, delete-orphan")


class B2BInvoiceItem(Base):
    __tablename__ = "b2b_invoice_items"

    id          = Column(Integer, primary_key=True, index=True)
    invoice_id  = Column(Integer, ForeignKey("b2b_invoices.id"), nullable=False)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty         = Column(Numeric(12,3), nullable=False)
    unit_price  = Column(Numeric(14,2), nullable=False)
    total       = Column(Numeric(14,2), nullable=False)

    invoice     = relationship("B2BInvoice", back_populates="items")
    product     = relationship("Product")


class Consignment(Base):
    __tablename__ = "consignments"

    id              = Column(Integer, primary_key=True, index=True)
    ref_number      = Column(String(30), unique=True, index=True)
    client_id       = Column(Integer, ForeignKey("b2b_clients.id"), nullable=False)
    invoice_id      = Column(Integer, ForeignKey("b2b_invoices.id"), nullable=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    status          = Column(String(20), default="active")  # active | settled | closed
    notes           = Column(Text)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    settled_at      = Column(DateTime(timezone=True), nullable=True)

    client          = relationship("B2BClient", back_populates="consignments")
    invoice         = relationship("B2BInvoice")
    user            = relationship("User")
    items           = relationship("ConsignmentItem", back_populates="consignment", cascade="all, delete-orphan")


class ConsignmentItem(Base):
    __tablename__ = "consignment_items"

    id                  = Column(Integer, primary_key=True, index=True)
    consignment_id      = Column(Integer, ForeignKey("consignments.id"), nullable=False)
    product_id          = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty_sent            = Column(Numeric(12,3), default=0)
    qty_sold            = Column(Numeric(12,3), default=0)
    qty_returned        = Column(Numeric(12,3), default=0)
    unit_price          = Column(Numeric(14,2), default=0)

    consignment         = relationship("Consignment", back_populates="items")
    product             = relationship("Product")


class B2BRefund(Base):
    __tablename__ = "b2b_refunds"

    id              = Column(Integer, primary_key=True, index=True)
    refund_number   = Column(String(30), unique=True, index=True)
    client_id       = Column(Integer, ForeignKey("b2b_clients.id"), nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    subtotal        = Column(Numeric(14,2), default=0)
    discount        = Column(Numeric(14,2), default=0)
    total           = Column(Numeric(14,2), default=0)
    notes           = Column(Text)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    client          = relationship("B2BClient")
    user            = relationship("User")
    items           = relationship("B2BRefundItem", back_populates="refund", cascade="all, delete-orphan")


class B2BRefundItem(Base):
    __tablename__ = "b2b_refund_items"

    id          = Column(Integer, primary_key=True, index=True)
    refund_id   = Column(Integer, ForeignKey("b2b_refunds.id"), nullable=False)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty         = Column(Numeric(12,3), nullable=False)
    unit_price  = Column(Numeric(14,2), nullable=False)
    total       = Column(Numeric(14,2), nullable=False)

    refund      = relationship("B2BRefund", back_populates="items")
    product     = relationship("Product")


class B2BClientPrice(Base):
    __tablename__ = "b2b_client_prices"
    __table_args__ = (UniqueConstraint("client_id", "product_id", name="uq_client_product_price"),)

    id          = Column(Integer, primary_key=True, index=True)
    client_id   = Column(Integer, ForeignKey("b2b_clients.id"), nullable=False)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    price       = Column(Numeric(14, 2), nullable=False)

    client      = relationship("B2BClient")
    product     = relationship("Product")