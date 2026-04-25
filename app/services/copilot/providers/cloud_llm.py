import re
import json
import httpx
import redis.asyncio as aioredis
from sqlalchemy import select, and_, func
from sqlalchemy.orm import joinedload
from app.core.log import logger
from app.core.config import settings

_http_client: httpx.AsyncClient | None = None
_redis_client = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=2.0,
                read=settings.ASSISTANT_LLM_TIMEOUT_SECONDS,
                write=5.0,
                pool=2.0,
            ),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )
    return _http_client


def _get_redis_client():
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
            decode_responses=True,
        )
    return _redis_client


def _trim_dashboard_context(dashboard_context: dict | None) -> dict | None:
    if not dashboard_context:
        return None

    trimmed_context = {}
    for k, v in dashboard_context.items():
        if k.startswith("_"):
            continue
        if isinstance(v, list):
            trimmed_context[k] = v[: settings.ASSISTANT_CONTEXT_LIST_LIMIT]
        elif isinstance(v, dict):
            trimmed_context[k] = {
                sk: sv[: settings.ASSISTANT_CONTEXT_LIST_LIMIT] if isinstance(sv, list) else sv
                for sk, sv in v.items()
                if not str(sk).startswith("_")
            }
        else:
            trimmed_context[k] = v

    serialized = json.dumps(trimmed_context, default=str, separators=(",", ":"))
    if len(serialized) > settings.ASSISTANT_MAX_CONTEXT_CHARS:
        raise ValueError("dashboard_context exceeds assistant size limits")
    return trimmed_context


async def _fetch_low_stock_inventory(db) -> list[dict]:
    from app.models.product import Product

    low_stock_threshold = func.coalesce(Product.reorder_level, Product.min_stock, 5)
    res = await db.execute(
        select(Product.name, Product.stock)
        .where(
            Product.is_active == True,
            Product.stock <= low_stock_threshold,
        )
        .order_by(Product.stock.asc(), Product.id.asc())
        .limit(settings.ASSISTANT_LOW_STOCK_LIMIT)
    )
    return [
        {"name": row.name, "stock": float(row.stock or 0)}
        for row in res.all()
    ]


async def _fetch_static_context(db, *, current_user_id: int | None) -> dict:
    static_context = None
    redis_client = None
    cache_key = f"copilot_static_context:{current_user_id or 'anon'}"

    try:
        redis_client = _get_redis_client()
        cached = await redis_client.get(cache_key)
        if cached:
            static_context = json.loads(cached)
    except Exception as e:
        logger.error(f"Redis cache read error in Copilot: {e}")

    if static_context:
        return static_context

    static_context = {
        "lifetime_sales": 0.0,
        "lifetime_expenses": 0.0,
        "outstanding_debt": [],
        "low_stock_inventory": [],
        "recent_expenses": []
    }

    try:
        from app.models.invoice import Invoice
        res = await db.execute(select(func.sum(Invoice.total)).where(Invoice.status == "paid"))
        static_context["lifetime_sales"] = float(res.scalar() or 0)
    except Exception as e:
        logger.error(f"Failed to fetch lifetime sales for AI context: {e}")

    try:
        from app.models.expense import Expense
        amount_col = getattr(Expense, "amount", getattr(Expense, "total", None))
        if amount_col is not None:
            res = await db.execute(select(func.sum(amount_col)))
            static_context["lifetime_expenses"] = float(res.scalar() or 0)
    except Exception as e:
        logger.error(f"Failed to fetch lifetime expenses for AI context: {e}")

    try:
        from app.models.b2b import B2BClient
        res = await db.execute(
            select(B2BClient)
            .where(B2BClient.is_active == True, B2BClient.outstanding > 0)
            .order_by(B2BClient.outstanding.desc())
            .limit(5)
        )
        static_context["outstanding_debt"] = [
            {"name": c.name, "amount": float(c.outstanding or 0)}
            for c in res.scalars().all()
        ]
    except Exception as e:
        logger.error(f"Failed to fetch outstanding debt for AI context: {e}")

    try:
        static_context["low_stock_inventory"] = await _fetch_low_stock_inventory(db)
    except Exception as e:
        logger.error(f"Failed to fetch low stock inventory for AI context: {e}")

    try:
        from app.models.expense import Expense
        stmt = select(Expense).order_by(Expense.id.desc()).limit(5)

        if hasattr(Expense, "category") and hasattr(getattr(Expense, "category"), "property"):
            stmt = stmt.options(joinedload(Expense.category))

        res = await db.execute(stmt)
        for expense in res.scalars().all():
            cat = getattr(expense, "category", None)
            cat_name = cat.name if hasattr(cat, "name") else str(cat) if cat else "Unknown"
            static_context["recent_expenses"].append({
                "category": cat_name,
                "amount": float(getattr(expense, "amount", getattr(expense, "total", 0)))
            })
    except Exception as e:
        logger.error(f"Failed to fetch recent expenses for AI context: {e}")

    if redis_client:
        try:
            await redis_client.setex(
                cache_key,
                settings.ASSISTANT_STATIC_CONTEXT_TTL_SECONDS,
                json.dumps(static_context),
            )
        except Exception as e:
            logger.error(f"Redis cache write error in Copilot: {e}")

    return static_context


async def _match_products_for_question(db, question: str) -> list[dict]:
    ignore_words = {
        "what", "show", "tell", "product", "products", "detail", "details", "find",
        "search", "about", "for", "the", "and", "how", "much", "many", "have",
        "we", "do", "does", "is", "are",
    }
    clean_question = re.sub(r"[^\w\s]", "", question).lower()
    words = clean_question.split()
    keywords = [
        word
        for word in words
        if word not in ignore_words and len(word) >= 3
    ][: settings.ASSISTANT_PRODUCT_KEYWORD_LIMIT]
    if not keywords:
        return []

    from app.models.product import Product

    conditions = [Product.name.ilike(f"%{kw}%") for kw in keywords]
    stmt = (
        select(Product.name, Product.stock, Product.price)
        .where(Product.is_active == True, and_(*conditions))
        .order_by(Product.name.asc())
        .limit(settings.ASSISTANT_PRODUCT_MATCH_LIMIT)
    )

    res = await db.execute(stmt)
    return [
        {
            "name": row.name,
            "stock": float(row.stock or 0),
            "price": float(row.price or 0),
        }
        for row in res.all()
    ]


class CloudCopilotProvider:
    async def answer(self, db, *, question: str, current_user, dashboard_context: dict | None = None) -> dict:
        if not settings.AI_API_KEY:
            return {
                "type": "text", 
                "content": "AI API key is not configured. Please set the AI_API_KEY environment variable on your server."
            }

        url = "https://api.groq.com/openai/v1/chat/completions"
        static_context = await _fetch_static_context(db, current_user_id=getattr(current_user, "id", None))

        deep_context = static_context.copy()
        deep_context["matched_products"] = []

        try:
            deep_context["matched_products"] = await _match_products_for_question(db, question)
        except Exception as e:
            logger.error(f"Failed to fetch dynamic product search for AI context: {e}")
            
        system_prompt = (
            "You are a business assistant. You have the Dashboard Summary (which is limited to a specific date range), "
            "as well as Global Lifetime Stats and Dynamic Product Search Results matching the user's question. "
            "Use the provided context to answer the user's questions accurately."
        )

        if dashboard_context:
            trimmed_context = _trim_dashboard_context(dashboard_context)
            system_prompt += f"\n\nHere is the Dashboard Summary:\n{json.dumps(trimmed_context, separators=(',', ':'))}"
            
        system_prompt += f"\n\nHere is the Deep Business Context:\n{json.dumps(deep_context, separators=(',', ':'))}"
            
        payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            "temperature": 0.3,
            "max_tokens": 512,
        }
        
        headers = {
            "Authorization": f"Bearer {settings.AI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        try:
            client = _get_http_client()
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
        except ValueError:
            return {"type": "text", "content": "The dashboard context was too large to process. Please refresh and try again."}
        except Exception as e:
            logger.exception("LLM API Error")
            return {"type": "text", "content": f"Connection failed: {str(e)}"}
