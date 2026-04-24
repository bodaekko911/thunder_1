import json
import httpx
from sqlalchemy import select
from app.core.log import logger
from app.core.config import settings


class CloudCopilotProvider:
    async def answer(self, db, *, question: str, current_user, dashboard_context: dict | None = None) -> dict:
        if not settings.AI_API_KEY:
            return {
                "type": "text", 
                "content": "AI API key is not configured. Please set the AI_API_KEY environment variable on your server."
            }

        url = "https://api.groq.com/openai/v1/chat/completions"
        
        deep_context = {
            "outstanding_debt": [],
            "low_stock_inventory": [],
            "recent_expenses": []
        }
        
        try:
            from app.models.b2b import B2BClient
            res = await db.execute(select(B2BClient).where(B2BClient.is_active == True, B2BClient.outstanding > 0))
            deep_context["outstanding_debt"] = [
                {"name": c.name, "phone": c.phone, "outstanding": float(c.outstanding or 0)}
                for c in res.scalars().all()
            ]
        except Exception as e:
            logger.error(f"Failed to fetch outstanding debt for AI context: {e}")
            
        try:
            from app.models.product import Product
            res = await db.execute(select(Product).where(Product.is_active == True))
            products = res.scalars().all()
            deep_context["low_stock_inventory"] = [
                {"name": p.name, "sku": p.sku, "stock": float(p.stock or 0), "min_stock": float(p.min_stock or 5)}
                for p in products
                if float(p.stock or 0) <= float(p.min_stock or 5)
            ]
        except Exception as e:
            logger.error(f"Failed to fetch low stock inventory for AI context: {e}")
            
        try:
            from app.models.expense import Expense
            res = await db.execute(select(Expense).order_by(Expense.id.desc()).limit(20))
            deep_context["recent_expenses"] = [
                {
                    "category": getattr(e, "category", "Unknown"),
                    "amount": float(getattr(e, "amount", getattr(e, "total", 0))),
                    "description": getattr(e, "description", getattr(e, "notes", "")),
                    "date": str(getattr(e, "date", getattr(e, "expense_date", getattr(e, "created_at", ""))))
                }
                for e in res.scalars().all()
            ]
        except Exception as e:
            logger.error(f"Failed to fetch recent expenses for AI context: {e}")
            
        system_prompt = (
            "You are a business assistant. Use the provided Dashboard Summary and the Deep Business Context "
            "to answer the user's questions accurately."
        )
        
        if dashboard_context:
            system_prompt += f"\n\nHere is the Dashboard Summary:\n{json.dumps(dashboard_context, indent=2)}"
            
        system_prompt += f"\n\nHere is the Deep Business Context:\n{json.dumps(deep_context, indent=2)}"
            
        payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            "temperature": 0.3,
            "max_tokens": 1024,
        }
        
        headers = {
            "Authorization": f"Bearer {settings.AI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return {"type": "text", "content": content}
                
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error communicating with Cloud LLM: {e.response.text}")
            return {"type": "text", "content": "I'm sorry, but the AI service returned an error. Please try again later."}
        except httpx.RequestError as e:
            logger.error(f"Network error communicating with Cloud LLM: {e}")
            return {"type": "text", "content": "I'm sorry, but I cannot reach the AI service right now."}
        except Exception as e:
            logger.exception("LLM API Error")
            return {"type": "text", "content": f"Connection failed: {str(e)}"}