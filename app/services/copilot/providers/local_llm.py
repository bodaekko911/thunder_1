import json
import httpx
from app.core.log import logger


class LocalCopilotProvider:
    async def answer(self, db, *, question: str, current_user, dashboard_context: dict | None = None) -> dict:
        url = "http://localhost:11434/v1/chat/completions"
        
        system_prompt = (
            "You are an AI assistant for an ERP system. Answer the user's questions clearly and concisely."
        )
        
        if dashboard_context:
            system_prompt += f"\n\nHere is the current dashboard context:\n{json.dumps(dashboard_context, indent=2)}"
            
        payload = {
            "model": "llama3",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ]
        }
        
        try:
            # Generous timeout for local LLM inference
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                
                return {"type": "text", "content": content}
                
        except httpx.RequestError as e:
            logger.error(f"Network error communicating with local LLM: {e}")
            return {"type": "text", "content": "I'm sorry, but I cannot reach the AI service right now. Please ensure the local Ollama server is running."}
        except Exception as e:
            logger.exception(f"Unexpected error in LocalCopilotProvider: {e}")
            return {"type": "text", "content": "I encountered an unexpected error while processing your request."}