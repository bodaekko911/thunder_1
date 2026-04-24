import json
import httpx
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
        
        system_prompt = (
            "You are an AI assistant for an ERP system. Answer the user's questions clearly and concisely."
        )
        
        if dashboard_context:
            system_prompt += f"\n\nHere is the current dashboard context:\n{json.dumps(dashboard_context, indent=2)}"
            
        payload = {
            "model": "llama3-8b-8192",
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
            logger.exception(f"Unexpected error in CloudCopilotProvider: {e}")
            return {"type": "text", "content": "I encountered an unexpected error while processing your request."}