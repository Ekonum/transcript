"""OpenAI text calls used by the web meeting pipeline."""
from __future__ import annotations

import json
from typing import Any

from cloud_transcription import CloudTranscriptionError, CloudUsage, OPENAI_API_BASE, OpenAITranscribeProvider

TEXT_MODEL = "gpt-5.4-2026-03-05"
REVIEW_MODEL = "gpt-6.2-astra"
DIARIZE_MODEL = "gpt-4o-transcribe-diarize"
CONTEXT_AUDIO_MODEL = "gpt-transcribe"


def strict_schema(schema: dict) -> dict:
    """The shared schemas also serve Gemini, which accepts uppercase types."""
    result = dict(schema)
    result["type"] = schema["type"].lower()
    if result["type"] == "object":
        result["properties"] = {k: strict_schema(v) for k, v in schema["properties"].items()}
        result.update(required=list(result["properties"]), additionalProperties=False)
    elif result["type"] == "array":
        result["items"] = strict_schema(schema["items"])
    return result


def text_cost(inputs: int, outputs: int, cached: int = 0, model: str = TEXT_MODEL) -> float:
    rates = (8, .4, 40) if model == REVIEW_MODEL else (2.5, .25, 15)
    return round(((inputs - cached) * rates[0] + cached * rates[1] + outputs * rates[2]) / 1_000_000, 6)


class OpenAITextClient(OpenAITranscribeProvider):
    def complete(self, messages: list[dict], *, model: str = TEXT_MODEL, **options: Any) -> tuple[dict, CloudUsage]:
        payload, _ = self._json(
            "POST", f"{OPENAI_API_BASE}/v1/chat/completions", headers=self._auth(),
            json_body={"model": model, "messages": messages, "store": False,
                       "reasoning_effort": "medium", **options},
        )
        choices = payload.get("choices") or []
        if not choices or choices[0].get("finish_reason") not in {"stop", "tool_calls"}:
            raise CloudTranscriptionError("Réponse OpenAI incomplète.", code="cloud_parse")
        msg = choices[0].get("message") or {}
        if msg.get("refusal"):
            raise CloudTranscriptionError("OpenAI a refusé cette analyse.")
        counters = payload.get("usage") or {}
        inputs, outputs = int(counters["prompt_tokens"]), int(counters["completion_tokens"])
        cached = int((counters.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        return msg, CloudUsage(model, inputs, outputs, text_cost(inputs, outputs, cached, model))

    def investigate(self, history: list[dict], instructions: str, tools: list[dict]):
        payload, _ = self._json(
            "POST", f"{OPENAI_API_BASE}/v1/responses", headers=self._auth(), json_body={
                "model": TEXT_MODEL, "store": False, "reasoning": {"effort": "medium"},
                "include": ["reasoning.encrypted_content"], "instructions": instructions,
                "input": history, "tools": [{"type": "function", "strict": False, **t} for t in tools],
                "tool_choice": "required", "parallel_tool_calls": False,
            },
        )
        if payload.get("status") != "completed":
            raise CloudTranscriptionError("Enquête OpenAI interrompue.")
        counters = payload["usage"]
        inputs, outputs = counters["input_tokens"], counters["output_tokens"]
        cached = (counters.get("input_tokens_details") or {}).get("cached_tokens", 0)
        return payload["output"], CloudUsage(TEXT_MODEL, inputs, outputs, text_cost(inputs, outputs, cached))

    def structured(self, instructions: str, data: str, schema: dict, *,
                   model: str = TEXT_MODEL) -> tuple[dict, CloudUsage]:
        msg, usage = self.complete(
            [{"role": "developer", "content": instructions},
             {"role": "user", "content": data}],
            model=model, response_format={"type": "json_schema", "json_schema": {
                "name": "meeting_analysis", "strict": True, "schema": strict_schema(schema)}},
        )
        try:
            obj = json.loads(msg["content"])
            if not isinstance(obj, dict):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudTranscriptionError("Réponse JSON OpenAI illisible.", code="cloud_parse") from exc
        return obj, usage
