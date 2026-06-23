# ruff: noqa
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
import google.auth
import httpx
import subprocess
import logging
from dotenv import load_dotenv
from typing import Optional, Any

from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

# Import A2A Config and Interceptor classes
from google.adk.a2a.agent.config import A2aRemoteAgentConfig, RequestInterceptor, ParametersConfig
from google.adk.agents.invocation_context import InvocationContext
from a2a.types import Message as A2AMessage
from google.adk.events.event import Event

# Monkeypatch ADK AgentCardBuilder to clean sub-agent skill names (no spaces or colons allowed!)
import google.adk.a2a.utils.agent_card_builder as card_builder
original_build_sub_agent_skills = card_builder._build_sub_agent_skills

async def my_build_sub_agent_skills(agent):
    skills = await original_build_sub_agent_skills(agent)
    cleaned_skills = []
    for skill in skills:
        from a2a.types import AgentSkill
        cleaned_skill = AgentSkill(
            id=skill.id,
            name=skill.name.replace(": ", "_").replace(":", "_").replace(" ", "_"),
            description=skill.description,
            examples=skill.examples,
            input_modes=skill.input_modes,
            output_modes=skill.output_modes,
            tags=skill.tags
        )
        cleaned_skills.append(cleaned_skill)
    return cleaned_skills

card_builder._build_sub_agent_skills = my_build_sub_agent_skills
logging.warning("🐒 Applied A2A AgentCardBuilder skill name monkeypatch successfully!")


# Configure logging for the agent
logger = logging.getLogger("bq_orchestrator_agent")
logger.setLevel(logging.WARNING)

# Load environment variables from app/.env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# Configure downstream remote agents cards paths from environment
mlb_agent_card_path = os.environ.get("MLB_AGENT_CARD")
nba_agent_card_path = os.environ.get("NBA_AGENT_CARD")

# 🐒 --- MONKEYPATCH A2A TASK CONVERTER --- 🐒
# The ADK's legacy task converter (event_converter.py) has a bug where it discards all
# but the last artifact: `parts=a2a_task.artifacts[-1].parts`.
# Since the Conversational Analytics agent returns multiple separate artifacts (intro, SQL, data table, insights),
# this bug discards the actual queried data table.
# We patch the function to aggregate all parts of all artifacts into a single unified message.

import google.adk.agents.remote_a2a_agent as remote_a2a_agent

def my_convert_a2a_task_to_event(
    a2a_task: Any,
    author: Optional[str] = None,
    invocation_context: Optional[InvocationContext] = None,
    part_converter: Any = None,
) -> Any:
    """Monkeypatched converter that aggregates ALL artifacts from the task
    instead of only keeping the last one.
    """
    if a2a_task and hasattr(a2a_task, "artifacts") and a2a_task.artifacts:
        logger.warning("🐒 [MONKEYPATCH] Aggregating all A2A task artifacts...")
        # Gather all parts from all artifacts
        all_parts = []
        for artifact in a2a_task.artifacts:
            if hasattr(artifact, "parts") and artifact.parts:
                all_parts.extend(artifact.parts)
        
        # Build a single unified message containing all the parts (intro, SQL, table, insights)
        from a2a.types import Message, Role
        message = Message(
            message_id="", 
            role=Role.agent, 
            parts=all_parts
        )
        
        from google.adk.a2a.converters.event_converter import convert_a2a_message_to_event
        return convert_a2a_message_to_event(
            message, author, invocation_context, part_converter=part_converter
        )
    
    # Fallback to the original legacy converter
    from google.adk.a2a.converters.event_converter import convert_a2a_task_to_event as original_converter
    return original_converter(a2a_task, author, invocation_context, part_converter)

# Apply the monkeypatch!
remote_a2a_agent.convert_a2a_task_to_event = my_convert_a2a_task_to_event
logger.warning("🐒 Applied A2A Task Converter monkeypatch successfully!")
# 🐒 -------------------------------------- 🐒

class GoogleAuth(httpx.Auth):
    """Custom HTTPX authentication handler that dynamically fetches and refreshes
    Google OAuth access tokens for both local development (gcloud CLI) and
    production (Application Default Credentials / Metadata Server).
    
    Ensures tokens never expire during long-running container lifecycles!
    """
    def __init__(self) -> None:
        self.credentials = None
        self.auth_request = None
        try:
            import google.auth
            self.credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            import google.auth.transport.requests
            self.auth_request = google.auth.transport.requests.Request()
        except Exception as e:
            logger.warning(f"Could not load Application Default Credentials: {e}")

    def auth_flow(self, request: httpx.Request) -> Any:
        token = None
        
        # 1. Try local gcloud CLI check first for backward compatibility
        try:
            token = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"], 
                text=True, 
                stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            # 2. Fallback to official Google Auth ADC libraries
            if self.credentials:
                try:
                    if not self.credentials.valid:
                        self.credentials.refresh(self.auth_request)
                    token = self.credentials.token
                except Exception as e:
                    logger.error(f"Failed to refresh Application Default Credentials: {e}")
                    
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
            
        yield request

def load_agent_card(path_or_json: str) -> Any:
    """Helper to load agent card from a local path, a GCS path, or a raw JSON string.
    
    Provides 100% production flexibility:
    - Dev: Reads relative local files.
    - Prod/CI: Reads GCS URIs or direct JSON strings from Secret Manager.
    """
    if not path_or_json:
        raise ValueError("Agent card path or content is empty.")
    
    import json
    from a2a.types import AgentCard
    
    card_dict = None
    
    # Case 1: Raw JSON string
    if path_or_json.strip().startswith("{"):
        card_dict = json.loads(path_or_json)
        
    # Case 2: Google Cloud Storage path (starts with gs://)
    elif path_or_json.startswith("gs://"):
        from google.cloud import storage
        client = storage.Client()
        bucket_name = path_or_json[5:].split("/")[0]
        blob_name = "/".join(path_or_json[5:].split("/")[1:])
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        card_dict = json.loads(blob.download_as_text())
        
    # Case 3: Local file path
    else:
        resolved_path = path_or_json
        if not os.path.isabs(resolved_path):
            resolved_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", path_or_json))
        with open(resolved_path, "r") as f:
            card_dict = json.load(f)
            
    return AgentCard.model_validate(card_dict)

# Build the client for local A2A call authentication
local_client = httpx.AsyncClient(auth=GoogleAuth(), timeout=600.0)

async def clean_routing_context_interceptor(
    ctx: InvocationContext, 
    a2a_request: A2AMessage, 
    params: ParametersConfig
) -> tuple[A2AMessage, ParametersConfig]:
    """Interceptor that strips out parent agent's routing/handshake events.
    
    Ensures the downstream A2A agent receives a clean, direct user question,
    preventing it from showing its welcome message instead of answering.
    """
    logger.warning("⚠️ --- [INTERCEPTOR START] --- ⚠️")
    original_part_texts = []
    for p in a2a_request.parts:
        if hasattr(p, "root") and hasattr(p.root, "text"):
            original_part_texts.append(f"Text: '{p.root.text}'")
        else:
            original_part_texts.append(str(p))
    logger.warning(f"Original parts: {original_part_texts}")

    cleaned_parts = []
    for part in a2a_request.parts:
        # Since Part is a Pydantic RootModel, check if the underlying root object is a TextPart
        if hasattr(part, "root") and hasattr(part.root, "text"):
            text_val = part.root.text
            if text_val:
                # Strip out any ADK-injected history helper text representing the routing actions.
                # We use a robust substring check to cover backticks injected by the framework.
                if (
                    text_val.startswith("For context:") or 
                    "transfer_to_agent" in text_val
                ):
                    logger.warning(f"🧹 Stripping part: {text_val}")
                    continue
        cleaned_parts.append(part)
    
    a2a_request.parts = cleaned_parts
    
    cleaned_part_texts = []
    for p in a2a_request.parts:
        if hasattr(p, "root") and hasattr(p.root, "text"):
            cleaned_part_texts.append(f"Text: '{p.root.text}'")
        else:
            cleaned_part_texts.append(str(p))
    logger.warning(f"Cleaned parts: {cleaned_part_texts}")
    logger.warning("⚠️ --- [INTERCEPTOR END] --- ⚠️")
    return a2a_request, params

def _log_message_parts(prefix: str, parts: list[Any]):
    """Helper to log parts of an A2A message."""
    logger.warning(f"  {prefix} parts count: {len(parts)}")
    for idx, part in enumerate(parts):
        if hasattr(part, "root"):
            root_part = part.root
            logger.warning(f"    Part {idx} type: {type(root_part).__name__}")
            if hasattr(root_part, "text"):
                logger.warning(f"    Part {idx} text: '{root_part.text}'")
            elif hasattr(root_part, "file") and root_part.file:
                file_obj = root_part.file
                name_val = getattr(file_obj, "name", "N/A")
                mime_val = getattr(file_obj, "mime_type", "N/A")
                logger.warning(f"    Part {idx} file -> name: {name_val}, mime: {mime_val}")
            elif hasattr(root_part, "data") and root_part.data:
                logger.warning(f"    Part {idx} data: {root_part.data}")

async def log_response_interceptor(
    ctx: InvocationContext, 
    a2a_response: Any, 
    event: Event
) -> Optional[Event]:
    """Interceptor that inspects the incoming A2A response payload from the downstream agent.
    
    Logs all parts (text, file, data) to find if tables/charts are present.
    Supports both A2AMessage and tuple (Task, TaskUpdate) responses.
    """
    logger.warning("📥 --- [RESPONSE INTERCEPTOR START] --- 📥")
    
    # Case 1: Response is a tuple (Task, TaskUpdate)
    if isinstance(a2a_response, tuple):
        task, update = a2a_response
        logger.warning(f"Response is a TUPLE. Task ID: {task.id if task else 'None'}")
        
        if task:
            logger.warning(f"Task State: {task.status.state if hasattr(task.status, 'state') else 'N/A'}")
            
            # Log messages in task status
            if task.status and hasattr(task.status, "message") and task.status.message:
                if hasattr(task.status.message, "parts") and task.status.message.parts:
                    _log_message_parts("Task Status Message", task.status.message.parts)
            
            # Log task artifacts
            if hasattr(task, "artifacts") and task.artifacts:
                logger.warning(f"  Task Artifacts count: {len(task.artifacts)}")
                for a_idx, artifact in enumerate(task.artifacts):
                    logger.warning(f"    Artifact {a_idx} ID: {artifact.artifact_id}")
                    if hasattr(artifact, "parts") and artifact.parts:
                        _log_message_parts(f"    Artifact {a_idx}", artifact.parts)
            else:
                logger.warning("  Task has NO artifacts.")
                
            # Log task history
            if hasattr(task, "history") and task.history:
                logger.warning(f"  Task History messages count: {len(task.history)}")
                for m_idx, msg in enumerate(task.history):
                    if hasattr(msg, "parts") and msg.parts:
                        _log_message_parts(f"    History Message {m_idx}", msg.parts)
        
        if update:
            logger.warning(f"Update Event Type: {type(update).__name__}")
            # Check if update has message parts
            if hasattr(update, "status") and update.status and hasattr(update.status, "message") and update.status.message:
                if hasattr(update.status.message, "parts") and update.status.message.parts:
                    _log_message_parts("Update Status Message", update.status.message.parts)
            # Check if update is artifact update
            if hasattr(update, "artifact") and update.artifact:
                if hasattr(update.artifact, "parts") and update.artifact.parts:
                    _log_message_parts("Update Artifact", update.artifact.parts)

    # Case 2: Response is a standard Message
    elif hasattr(a2a_response, "parts"):
        logger.warning("Response is a standard Message.")
        _log_message_parts("Message", a2a_response.parts)
        
    else:
        logger.warning(f"Response has unknown type: {type(a2a_response).__name__}")
        
    logger.warning("📥 --- [RESPONSE INTERCEPTOR END] --- 📥")
    return event

# Create remote agent config with our clean routing interceptor and response logger
remote_agent_config = A2aRemoteAgentConfig(
    request_interceptors=[
        RequestInterceptor(
            before_request=clean_routing_context_interceptor,
            after_request=log_response_interceptor
        )
    ]
)

# Instantiate RemoteA2aAgent for MLB Fan Experience
mlb_agent = RemoteA2aAgent(
    name="mlb_fan_experience",
    description="Conversational Analytics Agent for MLB Fan Experience. Handles analytical questions about MLB clubs, fan sentiment, stadium experience, attendance, and club engagement.",
    agent_card=load_agent_card(mlb_agent_card_path),
    httpx_client=local_client,
    config=remote_agent_config,
)

# Instantiate RemoteA2aAgent for NBA Player Stats
nba_agent = RemoteA2aAgent(
    name="nba_player_stats",
    description="Conversational Analytics Agent for NBA Player Stats. Handles analytical questions about NBA players, their performance, scoring, rebounds, assists, and game statistics.",
    agent_card=load_agent_card(nba_agent_card_path),
    httpx_client=local_client,
    config=remote_agent_config,
)

# Define precise orchestrator system instructions
instruction = """You are the BQ Orchestrator, a routing assistant that assesses and routes user analytical questions to downstream BigQuery Conversational Analytics (CA) Agents.

You have access to two downstream agents:
1. `nba_player_stats`: Handles questions and analysis related to NBA Player Stats (scoring, rebounds, assists, game statistics, player performance, etc.).
2. `mlb_fan_experience`: Handles questions and analysis related to MLB Fan Experience (clubs, fan sentiment, stadium experience, attendance, engagement, etc.).

Your behavior MUST strictly follow these rules:
1. **Assessment & Routing**:
   - If the user's question is about NBA Player Stats, immediately transfer/delegate the request to the `nba_player_stats` agent.
   - If the user's question is about MLB Fan Experience, immediately transfer/delegate the request to the `mlb_fan_experience` agent.
2. **Out-of-Domain Rejection**:
   - If the question is NOT related to NBA Player Stats or MLB Fan Experience, you MUST decline immediately. Respond EXACTLY: "The agents do not have context to respond to this question. Please ask about NBA Player stats or MLB Club Fan experience". Do NOT delegate, do NOT attempt to answer the question yourself, and do NOT add any extra text, pleasantries, or explanations.
3. **No Direct Execution**:
   - You must NOT directly query BigQuery or attempt to perform database operations yourself. You must always delegate to the respective BQ CA Agent.
4. **Response Integrity**:
   - When a downstream agent returns a response, relay the output of the agent back to the user exactly as received, without reinterpreting, summarizing, or modifying the content.
"""

# BQ Orchestrator root agent definition using the latest recommended model
root_agent = Agent(
    name="bq_orchestrator",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Orchestrator that routes sports analytical questions to specialized NBA and MLB Conversational Analytics agents.",
    instruction=instruction,
    sub_agents=[nba_agent, mlb_agent],
)

app = App(
    root_agent=root_agent,
    name="app",
)
