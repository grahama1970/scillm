import os
import asyncio
from dotenv import load_dotenv

from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from agents import Agent, Runner, ModelSettings, OpenAIChatCompletionsModel
from agents.mcp import MCPServerStdio, MCPServerStdioParams

load_dotenv(override=True)

# Cloud LLM (OpenAI). Do NOT set base_url here.
CLOUD_MODEL = os.getenv("CLOUD_MODEL", "gpt-5")
cloud_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
cloud_llm = OpenAIChatCompletionsModel(model=CLOUD_MODEL, openai_client=cloud_client)

# Codex Cloud MCP server (no -p ollama)
codex_cloud_params = MCPServerStdioParams({
    "command": "npx",
    "args": ["-y", "codex", "mcp-server"],
})

settings = ModelSettings(reasoning=Reasoning(effort="high"))

# Minimal instructions – you can replace with personas/CODER.md if you want
instructions = "You are a coding assistant. Use MCP tools when helpful."

agent = Agent(
    name="CloudCoder",
    instructions=instructions,
    model=cloud_llm,
    model_settings=settings,
)

async def main():
    async with MCPServerStdio(
        name="Codex-Cloud",
        params=codex_cloud_params,
        client_session_timeout_seconds=360000,
    ) as codex_cloud:
        agent.mcp_servers = [codex_cloud]

        # Simple task – the agent may call Codex tools via MCP if it needs them
        result = await Runner.run(agent, "Create a Python script that prints 'Hello, Codex Cloud!'", max_turns=3)
        print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())