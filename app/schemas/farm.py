from pydantic import BaseModel, Field
from typing import Optional, List


class DeliveryItemCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    notes:      Optional[str] = Field(None, max_length=500)


class DeliveryCreate(BaseModel):
    farm_id:       int
    delivery_date: str
    received_by:   Optional[str] = Field(None, max_length=200)
    quality_notes: Optional[str] = Field(None, max_length=500)
    notes:         Optional[str] = Field(None, max_length=500)
    items:         List[DeliveryItemCreate]
