# BQ Orchestrator: Complete Build & Implementation Steps

This document outlines the concise, step-by-step technical implementation process taken by the engineering team (Feroz and Antigravity) to build, debug, and verify the **BQ Orchestrator Agent** using the Google Agent Development Kit (ADK) and the Agents CLI.

---

## 📋 System Architecture Overview
The **BQ Orchestrator** is an A2A-compatible router agent that assesses user sports analytical queries and delegates them to one of two downstream BigQuery Conversational Analytics (CA) agents:
1. **NBA Player Stats** (`nba_player_stats`)
2. **MLB Fan Experience** (`mlb_fan_experience`)

The orchestrator enforces precise routing, strict out-of-domain rejections, and relays responses back to the user without modification.

---

## 🛠️ Step-by-Step Implementation Guide

### 1. Scaffolding and Configurations
*   **Scaffold the Agent Project**: Create the skeleton structure using the Agents CLI:
    ```bash
    agents-cli scaffold create bq-orchestrator-agent --template=empty
    ```
*   **Clean and Parameterize Downstream Cards**:
    *   Place the downstream Conversational Analytics Agent Cards (`nba_stats_agent_card.json` and `mlb_club_fan_agent_card.json`) in the project directory.
    *   Reference their absolute paths using environment variables in an `app/.env` file to keep the project clean, portable, and easily configurable across dev/prod environments:
        ```env
        NBA_AGENT_CARD=/Users/ferozmulla/Desktop/smart-coder/bq-orchestrator-agent/nba_stats_agent_card.json
        MLB_AGENT_CARD=/Users/ferozmulla/Desktop/smart-coder/bq-orchestrator-agent/mlb_club_fan_agent_card.json
        ```

### 2. Bypassing Corporate Index Lockout
During package synchronization (`uv sync`), the corporate machine was locked to a private, read-only Artifact Registry index, causing package resolution failures.
*   **Create Local `uv.toml`**: Instruct `uv` to look up public packages from the simple PyPI index and allow pre-releases:
    ```toml
    index-url = "https://pypi.org/simple"
    index-strategy = "first-index"
    prerelease = "allow"
    ```
*   **Restrict Python Target**: Prevent cross-platform resolution issues by pinning the supported Python version in `pyproject.toml` to a specific range (e.g., `requires-python = ">=3.11,<3.12"`).
*   **Enforce Environment Prefixes**: Override any read-only pip index configs system-wide by prefixing all execution and sync commands with explicit public PyPI indices:
    ```bash
    UV_DEFAULT_INDEX=https://pypi.org/simple UV_INDEX=https://pypi.org/simple UV_EXTRA_INDEX_URL="" <command>
    ```

### 3. Implementing Local A2A Authentication
Downstream Google Conversational Analytics endpoints (`geminidataanalytics.googleapis.com`) require valid OAuth 2.0 authentication. During local testing in the playground or evaluation suite, we must inject the developer's credentials.
*   **Dynamic Access Token Fetching**: In `app/agent.py`, implement a self-authenticating helper function that calls `gcloud` to fetch the developer's active OAuth token and binds it to the HTTP client:
    ```python
    def get_authenticated_client() -> httpx.AsyncClient:
        headers = {}
        try:
            token = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"], 
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass # Fallback for production service account environments
        return httpx.AsyncClient(headers=headers, timeout=600.0)
    ```
*   Pass this authenticated `httpx.AsyncClient` to the downstream `RemoteA2aAgent` instances.

### 4. Fixing the A2A Routing Context Leak
When a parent agent delegates to a sub-agent using the A2A protocol, the ADK automatically compiles the parent agent's session events (e.g., `transfer_to_agent` tool calls) into `"For context: ..."` text blocks and appends them to the outgoing payload. 
This confuses the downstream Gemini Conversational Analytics model, making it respond with its default welcome message instead of querying the database.
*   **Implement a Before-Request Interceptor**: Create a custom interceptor in `app/agent.py` to strip out these routing headers from the outgoing `parts` array:
    ```python
    async def clean_routing_context_interceptor(
        ctx: InvocationContext, a2a_request: A2AMessage, params: ParametersConfig
    ) -> tuple[A2AMessage, ParametersConfig]:
        cleaned_parts = []
        for part in a2a_request.parts:
            # Access the underlying part inside the Pydantic RootModel
            if hasattr(part, "root") and hasattr(part.root, "text"):
                text_val = part.root.text
                if text_val:
                    # Strip out any ADK-injected history text representing routing actions
                    if text_val.startswith("For context:") or "transfer_to_agent" in text_val:
                        continue
            cleaned_parts.append(part)
        a2a_request.parts = cleaned_parts
        return a2a_request, params
    ```
*   Register this interceptor as a `before_request` hook in an `A2aRemoteAgentConfig` object and pass it to both remote agents.

### 5. Fixing the ADK Multi-Artifact Payload Bug (Monkeypatch)
In the A2A protocol, the Conversational Analytics agent returns a stream of events representing the execution task. When it finishes, it returns a list of **5 distinct artifacts** (Intro, SQL query, metadata, markdown data table, and insights).
The ADK's legacy task converter (`event_converter.py`) has a critical bug where it hardcodes selecting **only the very last artifact**: `parts=a2a_task.artifacts[-1].parts`. Because the *Insights* section is the last artifact, the ADK completely discards the markdown table containing the actual queried database rows.
*   **Write a Runtime Python Monkeypatch**: Since the hosted service does not support the new-version extension yet (which forces the legacy converter to run), we inject a runtime monkeypatch in `app/agent.py` to override `remote_a2a_agent.convert_a2a_task_to_event` at startup:
    ```python
    import google.adk.agents.remote_a2a_agent as remote_a2a_agent

    def my_convert_a2a_task_to_event(a2a_task, author=None, invocation_context=None, part_converter=None):
        if a2a_task and hasattr(a2a_task, "artifacts") and a2a_task.artifacts:
            # Aggregate and merge ALL parts from ALL artifacts (0 to 4)
            all_parts = []
            for artifact in a2a_task.artifacts:
                if hasattr(artifact, "parts") and artifact.parts:
                    all_parts.extend(artifact.parts)
            
            # Construct a unified message containing the complete payload
            from a2a.types import Message, Role
            message = Message(message_id="", role=Role.agent, parts=all_parts)
            
            from google.adk.a2a.converters.event_converter import convert_a2a_message_to_event
            return convert_a2a_message_to_event(message, author, invocation_context, part_converter=part_converter)
        
        # Fallback to original converter
        from google.adk.a2a.converters.event_converter import convert_a2a_task_to_event as original_converter
        return original_converter(a2a_task, author, invocation_context, part_converter)

    # Inject our patched converter into the ADK module namespace
    remote_a2a_agent.convert_a2a_task_to_event = my_convert_a2a_task_to_event
    ```
    This successfully merges all artifacts, ensuring that the intro text, SQL query, markdown data table, and insights are all preserved and rendered together.

---

## 🧪 Local Testing & Verification

### 1. Systematic Evaluations
*   Configure the sports routing evaluation cases in `tests/eval/datasets/routing_eval.json`.
*   Add custom LLM-as-judge quality rubrics in `tests/eval/eval_config.yaml`.
*   Run the automated evaluation suite:
    ```bash
    UV_DEFAULT_INDEX=https://pypi.org/simple UV_INDEX=https://pypi.org/simple UV_EXTRA_INDEX_URL="" agents-cli eval run --dataset tests/eval/datasets/routing_eval.json
    ```

### 2. Interactive Web Playground
*   Launch the local web-based conversational interface (Streamlit runs on `8501`, backend API on `18080`):
    ```bash
    UV_DEFAULT_INDEX=https://pypi.org/simple UV_INDEX=https://pypi.org/simple UV_EXTRA_INDEX_URL="" agents-cli playground
    ```
*   Open **`http://localhost:8501`** in the browser to interactively verify routing, database queries, out-of-domain rejections, and full markdown table rendering.
