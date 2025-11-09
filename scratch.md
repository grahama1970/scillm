```zsh
MODEL="moonshotai/Kimi-K2-Instruct-0905"
#MODEL="moonshotai/Kimi-K2-Thinking"
curl -X POST \
  https://llm.chutes.ai/v1/chat/completions \
  -H "Authorization: Bearer $CHUTES_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'"${MODEL}"'",
    "messages": [
      {
        "role": "system",
        "content": "Only respond in well formatted JSON"
      },
      {
        "role": "user",
        "content": "What is the most common color of an apple? Respond with the following schema:\n{\n  fruit: <string>,\n  color: <string>\n}"
      }
    ],
    "stream": false,
    "max_tokens": 1024,
    "temperature": 0.7,
    "response_format": { "type": "json_object" }
  }'

  ```
