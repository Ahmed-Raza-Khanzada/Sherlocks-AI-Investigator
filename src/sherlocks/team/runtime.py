"""What every graph node shares: the model call, the event stream, and no tracing.

The case data never leaves the police network: LangGraph's optional LangSmith tracing is
switched off here, before any graph is built.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


# How every agent that writes for the officer must write.
PLAIN = ("Write in the plain words an investigating officer uses, and say what the records show. Never use "
         "network-analysis or data-science words - broker, gatekeeper, cluster, node, hub, centrality, betweenness, "
         "digital trail, compartmentalise: say instead 'X links two groups of people who have no other link', 'they "
         "have four contacts in common'. A SIM registered in another person's name is common and is not suspicious "
         "or a lead by itself. Call records show who called whom, when and through which tower - never what was "
         "said: never suggest reading the content of calls or messages. ")


# The case being worked is the one the officer describes - not the FIRs already on file.
NEW_CASE = ("THE NEW CASE is what the officer is investigating now, exactly as the officer described it (THE_NEW_CASE). "
            "The FIRs and other records already on file about the people are PAST RECORDS - other cases. Think as an "
            "investigator would: someone who says 'this man cheated me' is reporting a new matter, and the man's old "
            "FIRs are his history, not this matter. So never give a past FIR's date, place, offence, complainant or "
            "accused as the new case's, and never fill a gap in the new case from a past record. A past FIR is the new "
            "case's own FIR only when the officer gave its number as this case's FIR. There are three situations, and "
            "only the officer can say which: (1) the target has no earlier record - he is a suspect in a new matter; "
            "(2) the target has earlier cases - they are his history, and he is a suspect in a new matter; (3) the "
            "case being worked IS an FIR already on file in which the target is accused and was arrested - then that "
            "FIR, and only that FIR, describes this case. THE_NEW_CASE says which the officer told us (`which_case`, "
            "`target_status`); while it says 'not said yet', assume nothing - call him the target, not the accused, "
            "and do not say he was arrested. When the officer has not yet "
            "said what the new case is, say that it is not described yet - and speak of what is on file as past "
            "records. Until then form NO theory about it: no guess at its offence, who did it, why, or how a past "
            "record bears on it. What you may always do is describe the target, explain how people are connected "
            "to them, and set out their earlier cases - as records, not as a view on the case. A past record may still matter to the new case (the same people, the same kind of crime): say "
            "that as a link to check, not as part of the new case. ")


def _ask_every_field(schema: dict[str, Any]) -> None:
    schema["required"] = list(schema.get("properties") or {})


def asking(*names: str) -> ConfigDict:
    """For a schema with many small optional details (a register field): ask only for
    ``names``; the model may leave the others out, which keeps a long list short."""
    def only(schema: dict[str, Any]) -> None:
        schema["required"] = [n for n in names if n in (schema.get("properties") or {})]

    return ConfigDict(json_schema_extra=only)


class Typed(BaseModel):
    """A schema the model fills. Every field is marked required in the JSON schema the
    model is held to: with guided decoding a model simply leaves out any field that has a
    default (found on Qwen 27B: no answer shape, no depth, no sections came back). The
    defaults still apply when the object is validated, so nothing else has to give them."""

    model_config = ConfigDict(json_schema_extra=_ask_every_field)


def ask[T: BaseModel](llm: Any, schema: type[T], prompt: str, *, system: str, kind: str, version: str = "v1",
        max_tokens: int | None = None) -> T | None:
    """One typed answer from the model, or None (no model, or the call failed - the caller
    then leaves the work for the next run rather than guessing)."""
    if llm is None:
        return None
    try:
        out, _ = llm.generate_structured(prompt=prompt, schema=schema, system=system, cache_kind=kind,
                                         prompt_version=version, **({"max_tokens": max_tokens} if max_tokens else {}))
        return out
    except Exception as exc:  # noqa: BLE001 - a failed call is reported, never fatal to a run
        logger.info("Model call %s failed: %s", kind, exc)
        return None


def emit(event: dict[str, Any]) -> None:
    """Send an event to whoever streams this graph run (the chat's SSE). Outside a
    stream it is dropped."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(event)
    except Exception:  # noqa: BLE001 - no stream is listening
        return


_BRACKET = re.compile(r"\s*\[([^\[\]]{1,80})\]")
_ID = re.compile(r"[GDFLHQWXK]\d{1,4}")


def real_refs(text: str, known: set[str], systems: set[str] | None = None) -> str:
    """Keep a source in [brackets] only when it is real: an entry of the case board, or a
    police system's name. A model sometimes invents an id, or cites the name of a field:
    those brackets are taken out, the sentence stays."""
    from sherlocks.linkgraph.systems import SYSTEMS

    if systems is None:      # any police system; else only the ones the answer was actually given
        systems = {s.upper() for s in SYSTEMS} | {info.label.upper() for info in SYSTEMS.values()} | {"SIMS", "HOTEL EYE"}
    # The key a record carries ("HOTEL_EYE", "old_tenant") is written as the system's name ("Hotel Eye").
    named = {key.upper(): info.label for key, info in SYSTEMS.items()}

    def keep(m: re.Match[str]) -> str:
        good = []
        for part in (p.strip() for p in m.group(1).split(",")):
            head = part.split(" ")[0]
            if _ID.fullmatch(head) and head in known:
                good.append(part)
            elif part.upper() in systems:
                good.append(named.get(part.upper(), part))
        return f" [{', '.join(good)}]" if good else ""

    return _BRACKET.sub(keep, text or "").strip()


def dumps(value: Any, limit: int | None = None) -> str:
    import json

    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if limit is None else text[:limit]
