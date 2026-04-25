import re
import json
import httpx
from sqlalchemy import select, and_, func
from sqlalchemy.orm import joinedload
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
            "lifetime_sales": 0.0,
            "lifetime_expenses": 0.0,
            "outstanding_debt": [],
            "low_stock_inventory": [],
            "recent_expenses": [],
            "matched_products": []
        }
        
        try:
            from app.models.invoice import Invoice
            res = await db.execute(select(func.sum(Invoice.total)).where(Invoice.status == "paid"))
            deep_context["lifetime_sales"] = float(res.scalar() or 0)
        except Exception as e:
            logger.error(f"Failed to fetch lifetime sales for AI context: {e}")
            
        try:
            from app.models.expense import Expense
            amount_col = getattr(Expense, "amount", getattr(Expense, "total", None))
            if amount_col is not None:
                res = await db.execute(select(func.sum(amount_col)))
                deep_context["lifetime_expenses"] = float(res.scalar() or 0)
        except Exception as e:
            logger.error(f"Failed to fetch lifetime expenses for AI context: {e}")
            
        try:
            ignore_words = {"what", "show", "tell", "product", "products", "detail", "details", "find", "search", "about", "for", "the", "and", "how", "much", "many", "have", "we", "do", "does", "is", "are"}
            clean_question = re.sub(r'[^\w\s]', '', question).lower()
            words = clean_question.split()
            keywords = [w for w in words if w not in ignore_words and len(w) >= 3]
            
            if keywords:
                from app.models.product import Product
                conditions = [Product.name.ilike(f"%{kw}%") for kw in keywords]
                stmt = select(Product).where(Product.is_active == True, and_(*conditions)).limit(10)
                
                res = await db.execute(stmt)
                for p in res.scalars().all():
                    deep_context["matched_products"].append({
                        "name": p.name,
                        "stock": float(p.stock or 0),
                        "price": float(p.price or 0)
                    })
        except Exception as e:
            logger.error(f"Failed to fetch dynamic product search for AI context: {e}")

        try:
            from app.models.b2b import B2BClient
            res = await db.execute(
                select(B2BClient)
                .where(B2BClient.is_active == True, B2BClient.outstanding > 0)
                .order_by(B2BClient.outstanding.desc())
                .limit(5)
            )
            deep_context["outstanding_debt"] = [
                {"name": c.name, "amount": float(c.outstanding or 0)}
                for c in res.scalars().all()
            ]
        except Exception as e:
            logger.error(f"Failed to fetch outstanding debt for AI context: {e}")
            
        try:
            from app.models.product import Product
            res = await db.execute(select(Product).where(Product.is_active == True))
            products = res.scalars().all()
            low_stock = [
                {"name": p.name, "stock": float(p.stock or 0)}
                for p in products
                if float(p.stock or 0) <= float(p.min_stock or 5)
            ]
            low_stock.sort(key=lambda x: x["stock"])
            deep_context["low_stock_inventory"] = low_stock[:5]
        except Exception as e:
            logger.error(f"Failed to fetch low stock inventory for AI context: {e}")
            
        try:
            from app.models.expense import Expense
            stmt = select(Expense).order_by(Expense.id.desc()).limit(5)
            
            # Safely use joinedload if category is a mapped relationship to prevent greenlet_spawn errors
            if hasattr(Expense, "category") and hasattr(getattr(Expense, "category"), "property"):
                stmt = stmt.options(joinedload(Expense.category))
                
            res = await db.execute(stmt)
            for e in res.scalars().all():
                cat = getattr(e, "category", None)
                cat_name = cat.name if hasattr(cat, "name") else str(cat) if cat else "Unknown"
                deep_context["recent_expenses"].append({
                    "category": cat_name,
                    "amount": float(getattr(e, "amount", getattr(e, "total", 0)))
                })
        except Exception as e:
            logger.error(f"Failed to fetch recent expenses for AI context: {e}")
            
        system_prompt = (
            "You are a business assistant. You have the Dashboard Summary (which is limited to a specific date range), "
            "as well as Global Lifetime Stats and Dynamic Product Search Results matching the user's question. "
            "Use the provided context to answer the user's questions accurately."
        )
        
        if dashboard_context:
            trimmed_context = {}
            for k, v in dashboard_context.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, list):
                    trimmed_context[k] = v[:5]
                elif isinstance(v, dict):
                    trimmed_context[k] = {
                        sk: sv[:5] if isinstance(sv, list) else sv
                        for sk, sv in v.items()
                    }
                else:
                    trimmed_context[k] = v
                    
            system_prompt += f"\n\nHere is the Dashboard Summary:\n{json.dumps(trimmed_context, separators=(',', ':'))}"
            
        system_prompt += f"\n\nHere is the Deep Business Context:\n{json.dumps(deep_context, separators=(',', ':'))}"
            
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