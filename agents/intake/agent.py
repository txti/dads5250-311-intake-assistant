import json

import pandas as pd
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import START, MessagesState, StateGraph

from core.llm import parse
from core.models import Classification
from db.request_types import search

from datetime import datetime
from langchain_core.tools import tool
from db.request_types import DATA
import lancedb
from langgraph.prebuilt import ToolNode, tools_condition

PROMPT = """You are a Boston 311 intake assistant. From the candidate request types below, pick the single best match for the resident's message.

Rules:
- Only choose a type, department and queue from the provided list of request types.
- Set confidence to a number between 0.0 and 1.0, where 1.0 is very confident and 0.0 is not confident at all.
- If the message is vaue or could fit several types, set confidence below 0.6 and provide one short clarifying question to ask the resident.
- If the message is clear and you are sure it fits a single type, set confidence above 0.6 and leave clarifying_question empty (null).

Cadidate request types:
{context} """


class State(MessagesState):
    context: str


def retrieve(state: State) -> dict:
    # joins every user turn into one query, add an LLM query rewrite if follow-ups drift
    query = " ".join(m.text for m in state["messages"] if m.type == "human")
    hits = search(query, k=10)
    return {
        "context": "\n".join(
            f"- {h.type} ({h.reason}, {h.subject}): {h.department_name} ({h.department}), queue {h.queue}"
            for h in hits.itertuples()
        )
    }


def generate(state: State) -> dict:
    # Guard against bad input
    system = PROMPT.format(context=state["context"])

    content = state["messages"][-1].content if state["messages"] else ""
    user_text = content if isinstance(content, str) else str(content)
    user_text = user_text.strip()

    if not user_text:
        return {
            "messages": [AIMessage("Please describe the problem you'd like to report.")]
        }
    if len(user_text) > 2000:
        return {
            "messages": [
                AIMessage(
                    "Your message is too long. Please summarize it to 2000 characters or less."
                )
            ]
        }

    system = PROMPT.format(context=state["context"])

    # Retry once on API errors
    result = None
    for attempt in range(2):
        try:
            result = parse(Classification, system, user_text)
            break
        except Exception:
            if attempt == 1:
                result = None
    # Fall back to top search hit if the LLM fails
    if result is None:
        hits = search(user_text, k=1)
        if len(hits):
            row = hits.iloc[0]
            reply_text = (
                f"I couldn't confirm with the model, but the closest match is:\n"
                f"- Type: {row.type}\n"
                f"- Department: {row.department}\n"
                f"- Queue: {row.queue}\n"
                f"(unconfirmed)"
            )
        else:
            reply_text = (
                "Sorry, I couldn't find a matching request type. Please try rephrasing."
            )
        return {"messages": [AIMessage(reply_text)]}

    THRESHOLD = 0.6
    if result.confidence >= THRESHOLD or not result.clarifying_question:
        hits = search(result.type, k=1)
        row = hits.iloc[0] if len(hits) else None

        median = (
            f"{row.median_days_to_close:.0f} days"
            if row is not None and pd.notna(row.median_days_to_close)
            else "unknown"
        )
        on_time = (
            f"{row.on_time_rate_2024:.0%}"
            if row is not None and pd.notna(row.on_time_rate_2024)
            else "unknown"
        )

        reply_text = (
            f"I recommend: \n"
            f"Type: {result.type}\n"
            f"Department: {result.department}\n"
            f"Queue: {result.queue}\n"
            f"Confidence: {result.confidence:.0%}\n"
            f"Typical days to close: {median}\n"
            f"On-time rate (2024): {on_time}"
        )

        # File the request
        file_result = file_request.invoke(
            {
                "resident_message": user_text,
                "request_type": result.type,
                "department": result.department,
                "queue": result.queue,
                "confidence": result.confidence,
            }
        )
        reply_text += f"\n\n{file_result}"
    else:
        reply_text = f"Could you clarify: {result.clarifying_question}"

    return {"messages": [AIMessage(reply_text)]}


graph = (
    StateGraph(State)
    .add_node(retrieve)
    .add_node(generate)
    .add_edge(START, "retrieve")
    .add_edge("retrieve", "generate")
    .compile()
)


def ask(question: str, history: list[dict]) -> str:
    """`history` holds prior turns as {"role", "content"} dicts, so the caller owns the conversation."""
    state = graph.invoke({"messages": [*history, HumanMessage(question)]})
    return state["messages"][-1].content


if __name__ == "__main__":
    history = []
    while question := input("\nYou: ").strip():
        answer = ask(question, history)
        history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        print(f"\nBot: {answer}")


@tool
def file_request(
    resident_message: str,
    request_type: str,
    department: str,
    queue: str,
    confidence: float,
) -> str:
    """File a classified 311 request into the filed_requests table."""
    db = lancedb.connect(DATA / "lancedb")
    table = db.open_table("filed_requests")
    table.add(
        [
            {
                "resident_message": resident_message,
                "type": request_type,
                "department": department,
                "queue": queue,
                "confidence": confidence,
                "filed_at": datetime.now().isoformat(),
            }
        ]
    )
    return f"Filed request as {request_type} ({department})."


tools = [file_request]
