"""Shared output contracts for review/judge prompt rendering."""

NON_REACT_OUTPUT_CONTRACT = """Return a valid JSON object with this structure:
{
  "verdict": "APPROVED" | "CHANGES_SUGGESTED",
  "summary": "String overview of findings",
  "checklist": ["Task 1", "Task 2"],
  "comments": [
    {
      "anchor_id": "F1-L42",
      "severity": "CRITICAL|MAJOR|ADVISORY",
      "text": "Reasoning..."
    }
  ]
}"""

REACT_OUTPUT_CONTRACT = """Return strict JSON only using ONE of the following shapes:
1) Tool request:
   {"action":"tool_call","tool_name":"<tool>","arguments":{...},"reason_summary":"<one sentence>"}
2) Final output:
   {"action":"final_review","review_data":{"verdict":"APPROVED|CHANGES_SUGGESTED","summary":"...","checklist":[],"comments":[{"anchor_id":"F1-L42","severity":"CRITICAL|MAJOR|ADVISORY","text":"Reasoning..."}]}}
Do not output the bare review schema directly in ReAct mode."""
