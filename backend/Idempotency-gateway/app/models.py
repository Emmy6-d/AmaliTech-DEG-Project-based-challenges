from pydantic import BaseModel, Field

class PaymentRequest(BaseModel):
    amount: float = Field(..., gt=0, description="The payment amount, must be greater than 0.")
    currency: str = Field(..., min_length=3, max_length=3, pattern="^[A-Z]{3}$", description="3-character currency code, e.g. GHS.")
