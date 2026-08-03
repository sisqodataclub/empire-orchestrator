# llm.py
import os
import time
from openai import OpenAI

class NativeLLM:
    def __init__(self, api_key, base_url="https://api.deepseek.com", model="deepseek-chat", temperature=0.7):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature

    def call(self, messages):
        last_err = None
        for attempt, timeout_secs in enumerate([90, 120, 150], 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    timeout=timeout_secs,
                )
                return response.choices[0].message.content
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if any(kw in err_str for kw in ('401', '403', 'unauthorized', 'quota', 'billing')):
                    raise
                if attempt < 3:
                    time.sleep(5 * attempt)
        raise RuntimeError(f"LLM call failed after 3 attempts: {last_err}")
