import requests
import json
import os
from trainmate.config import config

class OpenRouterClient:
    def __init__(self):
        self.api_url = "https://openrouter.ai/api/v1/chat/completions"
        self.model = config.openrouter_model

    def complete(self, system_content, user_content):
        """Sends a request to OpenRouter using Gemini 3.5 Flash with structured JSON output."""
        api_key = config.openrouter_api_key
        if not api_key:
            raise ValueError("OpenRouter API key is not configured. Please set the OPENROUTER_API_KEY env variable or update config.json.")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
            "X-Title": "TrainMate Coach"
        }

        # Structure messages for prompt caching: large static content in system prompt
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content}
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"}
        }

        try:
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            resp_data = response.json()
            
            # Print token usage details for prompt caching verification
            usage = resp_data.get("usage", {})
            print(f"OpenRouter Tokens - Prompt: {usage.get('prompt_tokens')}, Completion: {usage.get('completion_tokens')}, Total: {usage.get('total_tokens')}")
            
            choices = resp_data.get("choices", [])
            if not choices:
                raise ValueError("Empty completion returned from OpenRouter.")
                
            content = choices[0].get("message", {}).get("content", "")
            return json.loads(content)
            
        except requests.exceptions.HTTPError as he:
            print(f"HTTP Error calling OpenRouter: {he}")
            if response is not None:
                print(f"Response Body: {response.text}")
            raise he
        except Exception as e:
            print(f"Error calling OpenRouter completions: {e}")
            raise e

# Singleton instance
openrouter_client = OpenRouterClient()
