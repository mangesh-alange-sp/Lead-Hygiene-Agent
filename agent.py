# agent.py
#
# The model only routes the file to the tool and repeats the counts.
# It never sees output rows and must not invent any.

from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from google.genai import types
from .pipeline.tools import run_dedup_pipeline

MAX_CSV_CHARS = 2_000_000


def dedup_guardrail_callback(tool, args, tool_context):
    if tool.name != "run_dedup_pipeline":
        return None

    csv_text = args.get("csv_text") or ""
    if len(csv_text) > MAX_CSV_CHARS:
        return {
            "status": "error",
            "message": f"CSV input too large ({len(csv_text)} chars). "
                        f"Split into batches under {MAX_CSV_CHARS} characters and run separately."
        }
    return None


SYSTEM_PROMPT = """
You are a lead hygiene router agent.
Your sole job is to accept lead CSV data and call run_dedup_pipeline.

1. Call run_dedup_pipeline with the uploaded CSV text. Nothing else.
2. If status is error, repeat the message and stop.
3. If status is ok, reply with exactly these lines, copying the numbers
   from the tool result:

deduped.csv is ready.
Leads in: <leads_in>
Validation issues: <validation_issues>
Values normalized: <values_normalized>
Duplicates merged: <duplicates_merged>
Leads to write back: <leads_out>

If blank_email_kept is greater than 0, add:
Leads with no email (kept, not merged): <blank_email_kept>

Do not add names, emails, tables, CSV text, or explanations.
"""

root_agent = Agent(
    name="lead_hygiene",
    model="gemini-2.5-flash",
    instruction=SYSTEM_PROMPT,
    tools=[
        FunctionTool(func=run_dedup_pipeline),
    ],
    before_tool_callback=dedup_guardrail_callback,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.0,
    ),
)
