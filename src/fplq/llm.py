"""One structured call to Claude on Bedrock.

Each stage of `fplq ask` needs a structured answer, not prose, so every call
forces exactly one tool and reads the tool's input back as the result. The
tool's JSON schema is the contract, and `toolChoice` makes the model use it
rather than answer in free text.

The model's output is untrusted: it reaches this module as data, and the
caller validates it (ask.py checks kinds and lengths; the SQL goes through
the validator) before anything acts on it.

`Chat` is a protocol so the pipeline can be tested with a scripted fake.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fplq.config import REPO_ROOT, Settings
from fplq.config import settings as default_settings

PROMPTS_DIR = REPO_ROOT / "prompts"


@dataclass(frozen=True)
class Prompt:
    """A system prompt and the version it is recorded under.

    The version is the file name (`generate.v1`), so a prompt change is a new
    file, and every logged call says which text produced it.
    """

    version: str
    text: str


def load_prompt(version: str, directory: Path = PROMPTS_DIR) -> Prompt:
    return Prompt(version=version, text=(directory / f"{version}.md").read_text().strip())


@dataclass(frozen=True)
class ToolResult:
    input: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class Chat(Protocol):
    model: str

    def call(self, system: str, messages: list[dict[str, Any]], tool: dict[str, Any],
             max_tokens: int) -> ToolResult: ...


class BedrockChat:
    """Claude through the Bedrock Converse API, temperature 0.

    Temperature 0 because the same question should get the same SQL: the
    golden set compares prompt versions, and sampling noise would blur the
    comparison. maxTokens is always explicit; left unset it reserves the
    model's maximum against the account's tokens-per-minute quota.
    """

    def __init__(self, settings: Settings = default_settings, client: Any | None = None,
                 model: str | None = None) -> None:
        self.model = model or settings.chat_model
        if client is None:
            import boto3  # optional dependency: pip install -e '.[aws]'
            from botocore.config import Config

            client = boto3.client(
                "bedrock-runtime",
                region_name=settings.bedrock_region,
                # The account's Claude quota is low (10 requests a minute for
                # Haiku 4.5), and adaptive retry waits rather than fails when it
                # is hit, so a response can take well over a minute to start.
                config=Config(retries={"total_max_attempts": 5, "mode": "adaptive"},
                              read_timeout=180),
            )
        self._client = client

    def call(self, system: str, messages: list[dict[str, Any]], tool: dict[str, Any],
             max_tokens: int) -> ToolResult:
        started = time.monotonic()
        response = self._client.converse(
            modelId=self.model,
            system=[{"text": system}],
            messages=messages,
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
            toolConfig={"tools": [{"toolSpec": tool}],
                        "toolChoice": {"tool": {"name": tool["name"]}}},
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        uses = [b["toolUse"] for b in response["output"]["message"]["content"] if "toolUse" in b]
        if response.get("stopReason") == "max_tokens" or not uses:
            raise RuntimeError(
                f"{self.model} did not complete a {tool['name']} call "
                f"(stopReason={response.get('stopReason')})"
            )
        usage = response.get("usage", {})
        return ToolResult(
            input=uses[0]["input"],
            model=self.model,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            latency_ms=latency_ms,
        )
