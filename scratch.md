```zsh
MODEL="moonshotai/Kimi-K2-Instruct-0905"
# MODEL="moonshotai/Kimi-K2-Thinking"
curl -X POST \
  https://llm.chutes.ai/v1/chat/completions \
  -H "Authorization: Bearer $CHUTES_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'"${MODEL}"'",
    "messages": [
      {
        "role": "system",
        "content": "Only respond in well formatted JSON. Fill the schema exactly: copy the single word the user asks about into the key 'object'; put all reasoning into 'thinking'; write the most common colour of that object into 'color'."
      },
      {
        "role": "user",
        "content": "What is the most common color of an apple?"
      }
    ],
    "stream": false,
    "max_tokens": 1024,
    "temperature": 0.7,
    "response_format": { "type": "json_object" }
  }' \
| jq -r '.choices[0].message.content' \
| python -m json_repair --indent 2
# | jq -r '.choices[0].message.content' \


# | python3 -c "import sys,json_repair,json; print(json.dumps(json_repair.loads(sys.stdin.read()),indent=2))"

# | jq -r '.choices[0].message.content'


# | python3 -c "
# import sys, json, json_repair
# raw = sys.stdin.read()
# try:
#     obj = json.loads(raw)          # try clean parse first
# except Exception:
#     obj = json_repair.loads(raw)   # fall back to repair
# print(json.dumps(obj, indent=2))
#   # | python3 -c "import sys,json_repair,json; print(json.dumps(json_repair.loads(sys.stdin.read()),indent=2))"
  # | jq
  # | jq -r '.choices[0].message.content' \

  ```


## Moonshot  Directly
```zsh
curl -X POST \
  https://api.moonshot.ai/v1/chat/completions \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kimi-k2-thinking",
    "messages": [
      {
        "role": "system",
        "content": "Only respond in well formatted JSON. Fill the schema exactly: copy the single word the user asks about into the key 'object'; put all reasoning into 'thinking'; write the most common colour of that object into 'color'."
      },
      {
        "role": "user",
        "content": "What is the most common color of an apple?"
      }
    ],
    "temperature": 0.7,
    "max_tokens": 1024,
    "top_p": 1,
    "stream": false,
    "response_format": { "type": "json_object" }
  }' \
| jq -r '.choices[0].message.content' \
| python -m json_repair --indent 2
```


Exactly — the **identical prompt** works on the **official Moonshot endpoint** but fails on **Chutes**, so the problem is **Chutes’ proxy**, not the Kimi model itself.

You can open a support ticket / GitHub issue with Chutes and point them to this reproducible difference:

---

**Title**  
“Kimi model returns `null` for placeholder fields when using OpenAI-compatible endpoint on Chutes, but works correctly on official Moonshot API”

**Curl pair to reproduce**  
1.  Moonshot (works):  
    ```bash
    curl -X POST https://api.moonshot.ai/v1/chat/completions \
      -H "Authorization: Bearer $MOONSHOT_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"model": "kimi-k2-thinking", "messages": [{"role": "system", "content": "Only respond in well formatted JSON. Fill the schema exactly: copy the single word the user asks about into the key 'object'; put all reasoning into 'thinking'; write the most common colour of that object into 'color'."}, {"role": "user", "content": "What is the most common color of an apple?"}], "temperature": 0.7, "max_tokens": 1024, "response_format": {"type": "json_object"}}' \
    | jq -r '.choices[0].message.content'
    ```
    → returns `"object": "apple"`

2.  Chutes (null):  
    ```bash
    curl -X POST https://llm.chutes.ai/v1/chat/completions \
      -H "Authorization: Bearer $CHUTES_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"model": "moonshotai/Kimi-K2-Instruct-0905", "messages": [{"role": "system", "content": "Only respond in well formatted JSON. Fill the schema exactly: copy the single word the user asks about into the key 'object'; put all reasoning into 'thinking'; write the most common colour of that object into 'color'."}, {"role": "user", "content": "What is the most common color of an apple?"}], "temperature": 0.7, "max_tokens": 1024, "response_format": {"type": "json_object"}}' \
    | jq -r '.choices[0].message.content'
    ```
    → returns `"object": null`

**Expected behaviour**  
Both calls should produce identical JSON content.

**Actual behaviour**  
Chutes endpoint returns `null` for placeholder fields while the official API fills them correctly.

**Request**  
Please investigate why the Chutes proxy alters the model’s output for the same request and, if possible, provide a switch or fix that preserves the literal-filling behaviour available on the official Moonshot endpoint.

