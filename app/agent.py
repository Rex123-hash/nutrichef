# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import json
import logging
from typing import Any
from pydantic import BaseModel, Field
from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.adk.workflow import Workflow, START
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.genai import types
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from .config import config

logger = logging.getLogger("security_checkpoint")

# -----------------------------------------------------------------------------
# 1. Pydantic Schemas
# -----------------------------------------------------------------------------

class UserRequest(BaseModel):
    user_message: str

class NutriChefState(BaseModel):
    user_message: str = ""
    diet_plan: dict = {}
    grocery_list: dict = {}
    orchestrator_output: str = ""
    estimated_cost: float = 0.0
    approved: bool = False
    security_passed: bool = True
    security_reason: str = ""

class DietPlan(BaseModel):
    dietary_preferences: str = Field(description="Summarized dietary preferences of the user.")
    weekly_meals: list[str] = Field(description="List of daily meals planned for the week.")
    notes: str = Field(description="Nutritionist advice or cooking tips.")

class GroceryList(BaseModel):
    items: list[str] = Field(description="Categorized list of groceries needed.")
    estimated_cost: float = Field(description="Estimated cost of the groceries in USD.")
    notes: str = Field(description="Budget saving tips or suggestions.")

# -----------------------------------------------------------------------------
# 2. Specialized LlmAgents & Orchestrator
# -----------------------------------------------------------------------------

current_dir = os.path.dirname(os.path.abspath(__file__))
mcp_server_path = os.path.join(current_dir, "mcp_server.py")

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", mcp_server_path],
        ),
    ),
)

diet_planner = LlmAgent(
    name="diet_planner",
    model=Gemini(model=config.model),
    instruction="""You are a professional nutritionist and meal planner. 
Based on the user's dietary preferences (e.g. vegan, keto, allergies) and request,
create a weekly meal plan structure. Keep the plan realistic and budget-friendly.
You can query available recipes using the search_recipes tool.""",
    tools=[mcp_toolset],
)

grocery_specialist = LlmAgent(
    name="grocery_specialist",
    model=Gemini(model=config.model),
    instruction="""You are a grocery shopping and pantry expert.
Given a meal plan and dietary preferences, list all necessary grocery items.
Provide an estimated cost for the list in USD.
You MUST check the pantry inventory using get_pantry_items to avoid listing items the user already has.
At the end of your response, ALWAYS output the estimated cost in this format: 'Estimated Cost: $XX.XX'.""",
    tools=[mcp_toolset],
)

orchestrator = LlmAgent(
    name="orchestrator",
    model=Gemini(model=config.model),
    instruction="""You are the NutriChef Coordinator. Your task is to coordinate the diet planner and grocery specialist agents.
When a user asks for a meal plan, grocery list, or budget check, delegate:
1. Use the diet_planner tool to create the meal plan.
2. Use the grocery_specialist tool to generate the grocery list and calculate estimated cost.
Combine the outputs and summarize the complete proposal for the user.""",
    tools=[AgentTool(diet_planner), AgentTool(grocery_specialist)],
    output_key="orchestrator_output",
)

# -----------------------------------------------------------------------------
# 3. Workflow Nodes
# -----------------------------------------------------------------------------

def security_checkpoint(ctx: Context, node_input: types.Content) -> Event:
    text = ""
    if hasattr(node_input, 'parts') and node_input.parts:
        text = "".join(part.text for part in node_input.parts if hasattr(part, 'text') and part.text)
    elif isinstance(node_input, str):
        text = node_input
        
    state_updates = {"user_message": text}
    
    # 1. PII Scrubbing
    pii_patterns = {
        "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "phone": r"\+?[\d\-\s\(\)]{8,15}",
    }
    
    scrubbed_text = text
    scrubbed_fields = []
    
    for pii_type, pattern in pii_patterns.items():
        matches = re.findall(pattern, scrubbed_text)
        if matches:
            scrubbed_fields.append(pii_type)
            scrubbed_text = re.sub(pattern, f"[{pii_type.upper()}]", scrubbed_text)
            
    if scrubbed_fields:
        state_updates["user_message"] = scrubbed_text
        audit_log = {
            "event": "pii_scrubbed",
            "session_id": ctx.session.id,
            "fields": scrubbed_fields,
            "severity": "WARNING"
        }
        logger.warning(json.dumps(audit_log))
    else:
        audit_log = {
            "event": "pii_check_passed",
            "session_id": ctx.session.id,
            "severity": "INFO"
        }
        logger.info(json.dumps(audit_log))

    # 2. Prompt Injection Detection
    injection_keywords = ["bypass", "ignore previous", "system prompt", "developer mode", "jailbreak", "override"]
    detected_keywords = [kw for kw in injection_keywords if kw in scrubbed_text.lower()]
    
    if detected_keywords:
        state_updates.update({
            "security_passed": False,
            "security_reason": f"Prompt injection attempt detected: {', '.join(detected_keywords)}"
        })
        audit_log = {
            "event": "prompt_injection_blocked",
            "session_id": ctx.session.id,
            "keywords": detected_keywords,
            "severity": "CRITICAL"
        }
        logger.critical(json.dumps(audit_log))
        return Event(
            output="Security Check Failed: Prompt injection attempt detected.",
            route="SECURITY_EVENT",
            state=state_updates
        )

    # 3. Domain Specific Safety Rules (dangerous diets / non-food toxic substances)
    dangerous_substances = ["poison", "bleach", "cyanide", "arsenic", "starvation"]
    detected_substances = [sub for sub in dangerous_substances if sub in scrubbed_text.lower()]
    
    if detected_substances:
        state_updates.update({
            "security_passed": False,
            "security_reason": f"Hazardous query blocked: mentions dangerous substances/diets ({', '.join(detected_substances)})"
        })
        audit_log = {
            "event": "dangerous_substance_blocked",
            "session_id": ctx.session.id,
            "substances": detected_substances,
            "severity": "CRITICAL"
        }
        logger.critical(json.dumps(audit_log))
        return Event(
            output="Security Check Failed: Dangerous substances or extreme starvation diets are not allowed.",
            route="SECURITY_EVENT",
            state=state_updates
        )
        
    # All checks passed
    audit_log = {
        "event": "security_check_passed",
        "session_id": ctx.session.id,
        "severity": "INFO"
    }
    logger.info(json.dumps(audit_log))
    return Event(
        output=scrubbed_text,
        route="clean",
        state=state_updates
    )

async def hitl_approval(ctx: Context, node_input: Any) -> Event:
    orchestrator_output = ctx.state.get("orchestrator_output", "")
    match = re.search(r"Estimated Cost[^\d]*\$?([\d]+(?:[.,]\d+)*)", orchestrator_output, re.IGNORECASE)
    estimated_cost = 0.0
    if match:
        try:
            cost_str = match.group(1).rstrip(".").replace(",", "")
            estimated_cost = float(cost_str)
        except Exception:
            pass
            
    state_updates = {"estimated_cost": estimated_cost}
    
    if estimated_cost > 50.0:
        if not ctx.resume_inputs or "approved" not in ctx.resume_inputs:
            return RequestInput(
                interrupt_id="approved",
                message=f"Estimated grocery cost is ${estimated_cost:.2f}, which exceeds the $50 budget limit. Do you approve? (yes/no)"
            )
        
        user_response = ctx.resume_inputs["approved"]
        if "yes" in str(user_response).lower():
            state_updates["approved"] = True
            return Event(
                output=f"Budget of ${estimated_cost:.2f} approved by user. Proceeding.",
                state=state_updates
            )
        else:
            state_updates["approved"] = False
            return Event(
                output=f"Budget of ${estimated_cost:.2f} rejected by user. Proposal aborted.",
                state=state_updates
            )
    else:
        state_updates["approved"] = True
        return Event(
            output="Budget within limits.",
            state=state_updates
        )

def final_output(ctx: Context, node_input: str) -> Event:
    security_passed = ctx.state.get("security_passed", True)
    if not security_passed:
        reason = ctx.state.get("security_reason", "Security check failed.")
        error_text = f"### ⚠️ Security Block\n\n{reason}"
        return Event(
            content=types.Content(
                role='model',
                parts=[types.Part.from_text(text=error_text)]
            ),
            output=error_text
        )
        
    orchestrator_output = ctx.state.get("orchestrator_output", "")
    approved = ctx.state.get("approved", False)
    estimated_cost = ctx.state.get("estimated_cost", 0.0)
    
    status_str = "APPROVED" if approved else "REJECTED (Budget Exceeded)"
    
    response_text = f"""
## NutriChef Plan Status: {status_str}

### Coordination Summary:
{orchestrator_output}

---
*Estimated cost: ${estimated_cost:.2f}*
"""
    return Event(
        content=types.Content(
            role='model',
            parts=[types.Part.from_text(text=response_text)]
        ),
        output=response_text
    )

# -----------------------------------------------------------------------------
# 4. Workflow Graph Definition
# -----------------------------------------------------------------------------

root_agent = Workflow(
    name="nutrichef_workflow",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {'clean': orchestrator}),
        (security_checkpoint, {'SECURITY_EVENT': final_output}),
        (orchestrator, hitl_approval),
        (hitl_approval, final_output),
    ],
    state_schema=NutriChefState,
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
