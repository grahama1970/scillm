# pip install openai
# Usage:
#   export OPENAI_API_KEY="cpk_..."
#   export CHUTES_API_BASE_URL="https://api.chutes.ai/v1"   # or your chute URL
#   python examples/python/openai_client.py

import os
from openai import OpenAI

BASE_URL = os.getenv("CHUTES_API_BASE_URL", "https://api.chutes.ai/v1")
API_KEY = os.getenv("OPENAI_API_KEY")  # cpk_...

if not API_KEY:
    raise SystemExit("Set OPENAI_API_KEY to your chutes API key (cpk_...).")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

def chat():
    resp = client.chat.completions.create(
        model=os.getenv("CHUTES_MODEL", "your-model"),
        messages=[{"role": "user", "content": "Say hello!"}],
        stream=False,
    )
    print("Chat:", resp.choices[0].message.content)

def embed():
    resp = client.embeddings.create(
        model=os.getenv("CHUTES_EMBEDDING_MODEL", "your-embedding-model"),
        input=["test input for embeddings"],
    )
    print("Embedding length:", len(resp.data[0].embedding))

if __name__ == "__main__":
    chat()
    embed()
