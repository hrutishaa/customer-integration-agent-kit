"""Run the tool-calling agent against the mock Acme CRM.

    python examples/agent_demo.py            # offline, scripted model, no key
    LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=... python examples/agent_demo.py
    LLM_MODEL=llama3.1 LLM_BASE_URL=http://localhost:11434/v1 python examples/agent_demo.py

Writes always stop at an approval prompt in the terminal.
"""
from __future__ import annotations

import os

from agentkit import CRMConnector
from agentkit.agent import (
    Agent,
    ModelResponse,
    OpenAICompatibleProvider,
    ScriptedProvider,
    ToolRegistry,
    call,
    connector_tools,
)
from agentkit.mock import AcmeCRM

TASK = "Mark ada@acme.com as won and make sure grace@acme.com exists in the CRM."


def model():
    name = os.environ.get("LLM_MODEL")
    if not name:
        return ScriptedProvider([
            ModelResponse(tool_calls=[call("crm_read", resource="contacts",
                                           filters={"email": "ada@acme.com"})]),
            ModelResponse(tool_calls=[call("crm_write", resource="contacts", upsert_on="email",
                                           records=[{"email": "ada@acme.com", "stage": "won"},
                                                    {"email": "grace@acme.com",
                                                     "name": "Grace Hopper"}])]),
            ModelResponse(content="Ada is now 'won' and Grace was added."),
        ])
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    key_env = None if "localhost" in base else "OPENAI_API_KEY"
    return OpenAICompatibleProvider(name, base_url=base, api_key_env=key_env)


def approve(tool, args) -> bool:
    print(f"\nThe agent wants to call {tool.name} with:\n  {args}")
    return input("Approve? [y/N] ").strip().lower() == "y"


def main() -> None:
    crm = AcmeCRM()
    crm.seed("contacts", [{"email": "ada@acme.com", "name": "Ada Lovelace", "stage": "lead"}])
    os.environ.setdefault("ACME_TOKEN", "acme-test-token")
    conn = CRMConnector("https://acme.test", token_env="ACME_TOKEN", transport=crm.transport)
    tools = ToolRegistry().add(*connector_tools(conn, resources=["contacts"]))
    trace = Agent(model(), tools, confirm=approve, max_tool_calls=6).run(TASK)
    print("\n" + trace.summary())
    print("\nAnswer:", trace.answer)
    print("CRM now:", [(r["email"], r.get("stage")) for r in crm.all("contacts")])


if __name__ == "__main__":
    main()
