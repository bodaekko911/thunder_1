from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Recipe(Base):
    """A saved formula — reusable starting point for batches."""
    __tablename__ = "recipes"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(200), nullable=False)
    description = Column(Text)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    inputs  = relationship("RecipeInput",  back_populates="recipe", cascade="all, delete-orphan")
    outputs = relationship("RecipeOutput", back_populates="recipe", cascade="all, delete-orphan")
    batches = relationship("ProductionBatch", back_populates="recipe")


class RecipeInput(Base):
    """A raw material used in a recipe."""
    __tablename__ = "recipe_inputs"

    id         = Column(Integer, primary_key=True, index=True)
    recipe_id  = Column(Integer, ForeignKey("recipes.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)

    recipe  = relationship("Recipe", back_populates="inputs")
    product = relationship("Product")


class RecipeOutput(Base):
    """A finished product produced by a recipe."""
    __tablename__ = "recipe_outputs"

    id         = Column(Integer, primary_key=True, index=True)
    recipe_id  = Column(Integer, ForeignKey("recipes.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)

    recipe  = relationship("Recipe", back_populates="outputs")
    product = relationship("Product")


class ProductionBatch(Base):
    """One actual processing run."""
    __tablename__ = "production_batches"

    id           = Column(Integer, primary_key=True, index=True)
    batch_number = Column(String(30), unique=True, index=True)
    recipe_id    = Column(Integer, ForeignKey("recipes.id"), nullable=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    status       = Column(String(20), default="completed")
    waste_pct    = Column(Numeric(5, 2), default=0)
    notes        = Column(Text)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    recipe  = relationship("Recipe", back_populates="batches")
    user    = relationship("User")
    inputs  = relationship("BatchInput",  back_populates="batch", cascade="all, delete-orphan")
    outputs = relationship("BatchOutput", back_populates="batch", cascade="all, delete-orphan")


class BatchInput(Base):
    """Raw material consumed in a batch."""
    __tablename__ = "batch_inputs"

    id         = Column(Integer, primary_key=True, index=True)
    batch_id   = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)

    batch   = relationship("ProductionBatch", back_populates="inputs")
    product = relationship("Product")


class BatchOutput(Base):
    __tablename__ = "batch_outputs"

    id       = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty      = Column(Numeric(12, 3), nullable=False)

    product = relationship("Product")
    batch   = relationship("ProductionBatch", back_populates="outputs")
