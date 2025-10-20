# gemini_rest_example.py
import os
import requests
import json

GEN_AI_API_URL = os.environ.get("GEN_AI_API_URL")  # e.g. "https://generativemodels.googleapis.com/v1/models/xxx:generateText"
GEN_AI_API_KEY = os.environ.get("GEN_AI_API_KEY")  # if using API key
OAUTH_TOKEN = os.environ.get("OAUTH_TOKEN")  # if using OAuth Bearer

def call_gemini_rest(prompt: str):
    headers = {"Content-Type": "application/json"}
    if OAUTH_TOKEN:
        headers["Authorization"] = f"Bearer {OAUTH_TOKEN}"
    elif GEN_AI_API_KEY:
        headers["x-api-key"] = GEN_AI_API_KEY

    payload = {"prompt": prompt, "max_output_tokens": 1024, "temperature": 0.2}
    resp = requests.post(GEN_AI_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    text = data.get("output", {}).get("text") or data.get("generated_text") or json.dumps(data)
    return text

if __name__ == "__main__":
    prompt = "Explain a child's EEG with short alpha surges in plain language and include evidence tags."
    print(call_gemini_rest(prompt))
