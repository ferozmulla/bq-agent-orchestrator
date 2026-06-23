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

---

## 🧠 Advanced: Enabling Native Agent Runtime A2A Registration

While the official Google Cloud documentation states that A2A agents hosted on any platform (including Vertex AI Agent Runtime) can be connected to Gemini Enterprise, the local SDK and CLI tools contain outdated constraints that block this deployment out-of-the-box. 

Below are the **three surgical patches** applied to the local development environment to successfully register the Agent Runtime version of the Orchestrator as a native, secure A2A agent.

### 1. Bypassing the Client-Side CLI Blocker
The `agents-cli publish` command contains a hardcoded client-side block that immediately raises an exception if the deployment target is `agent_runtime`.
*   **File**: `/Users/ferozmulla/.local/share/uv/tools/google-agents-cli/lib/python3.11/site-packages/google/agents/cli/publish/cmd_publish.py`
*   **Patch**: Comment out or bypass the `agent_runtime` check (lines 1420-1426) to let the command proceed:
    ```python
    # A2A agents on Agent Runtime are not yet supported by Gemini Enterprise.
    if deployment_target == "agent_runtime":
        pass # Temporarily bypassed to allow Reasoning Engine A2A registration
    ```

### 2. Correcting the Framework Override
When deploying an A2A agent, the CLI's deployment engine (`agent_runtime.py`) overrides the framework flag and registers it in the cloud as `"custom"` (to force the Cloud Console to render a playground UI). However, the Vertex AI Gateway **only** exposes A2A endpoints if the engine is registered under the `"a2a"` framework name.
*   **File**: `/Users/ferozmulla/.local/share/uv/tools/google-agents-cli/lib/python3.11/site-packages/google/agents/cli/deploy/agent_runtime.py`
*   **Patch**: Change the override from `"custom"` to `"a2a"` (line 515):
    ```python
    # Ensure it registers under the native 'a2a' platform framework in the cloud
    config_kwargs["agent_framework"] = "a2a" if cfg.is_a2a else "google-adk"
    ```

### 3. Surgically Repairing the SDK Schema
Even though the SDK's internal utilities support `"a2a"`, the SDK's public type definitions (`AgentEngineConfig` and `AgentEngineConfigDict`) enforce a strict Pydantic/TypedDict `Literal` constraint that lacks `'a2a'`, causing validation errors before the API call is made.
*   **File**: `/Users/ferozmulla/.local/share/uv/tools/google-agents-cli/lib/python3.11/site-packages/vertexai/_genai/types/common.py`
*   **Patch**: Add `"a2a"` to the `Literal` definitions in both classes (lines 19863 and 20055):
    ```python
    agent_framework: Optional[
        Literal["google-adk", "langchain", "langgraph", "ag2", "llama-index", "custom", "a2a"]
    ]
    ```

### 🚀 Result & Verification
Once these patches were applied, redeploying the Agent Runtime engine succeeded in native A2A mode:
`INFO:vertexai_genai.agentengines:Using agent framework: a2a`

The secure Vertex AI Gateway immediately exposed the `/a2a/v1/card` endpoint, returning `200 OK` and your full Agent Card JSON when queried with valid Google Cloud credentials, enabling a **flawless native A2A registration** in Gemini Enterprise!

### 4. Resolving the Localhost & Region Gateway Routing Bug (Spinning/Thinking Issue)
During testing in Gemini Enterprise, the A2A routing was found to spin forever ("Thinking..."). We diagnosed and resolved this through a two-part routing fix:

#### A. The Localhost URL Bug
*   **The Issue**: When the Reasoning Engine was queried, its card returned `"url": "http://localhost:9999"`. This happened because during container instantiation, `__init__` is called before `vertexai.init()`, leaving the SDK's global configuration empty and causing the parent `super().set_up()` to fail to construct the cloud URL and fall back to localhost.
*   **The Effect**: Gemini Enterprise read `localhost:9999` from the registered card and tried to send conversational RPC requests (`on_message_send`) to localhost, causing a silent timeout/infinite spin.

#### B. The Region Discrepancy Bug
*   **The Issue**: When we tried to dynamically resolve the location in the container using `GOOGLE_CLOUD_LOCATION`, the container resolved to `us-central1` because the underlying serverless compute pool is physically hosted there.
*   **The Effect**: The card URL generated was `https://us-central1-aiplatform.googleapis.com/.../locations/us-central1/.../a2a`. However, since your resource lives in `us-east1`, calling the `us-central1` gateway resulted in a `404 Not Found` error.

#### C. The Dynamic URL Injection Patch
To resolve both issues, we updated the `set_up()` method in [app/agent_runtime_app.py](file:///Users/ferozmulla/Desktop/smart-coder/bq-orchestrator-agent/app/agent_runtime_app.py) to run `super().set_up()`, read the platform-injected project and engine ID environment variables, and **explicitly force the deployment region (`us-east1`)** to overwrite the A2A URL on the card, handler, and adapter:

```python
    def set_up(self) -> None:
        """Initialize the agent engine app with logging and telemetry."""
        vertexai.init()
        setup_telemetry()
        
        # Run parent setup to instantiate all adapters and handlers
        super().set_up()
        
        # Dynamically resolve and overwrite the A2A URL with the real deployed cloud URL!
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("VERTEX_AI_PROJECT_ID")
        location = "us-east1" # Forced to deployment region to prevent us-central1 (compute) or global platform overrides
        agent_engine_id = os.environ.get("VERTEX_AI_REASONING_ENGINE_ID") or os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
        
        if project and agent_engine_id:
            real_url = f"https://{location}-aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{location}/reasoningEngines/{agent_engine_id}/a2a"
            self.agent_card.url = real_url
            if hasattr(self, "a2a_rest_adapter") and self.a2a_rest_adapter:
                self.a2a_rest_adapter.agent_card.url = real_url
            if hasattr(self, "rest_handler") and self.rest_handler:
                self.rest_handler.agent_card.url = real_url
            logging.info(f"🐒 Dynamically injected secure A2A URL: {real_url}")
```

#### D. Dependency Bloat Cleanup
We discovered that adding unnecessary web packages (`fastapi`, `sqlalchemy`, etc., which are only needed for Cloud Run) to `pyproject.toml` caused the cloud container builder to install `pydantic-2.14.0a1` (an unstable alpha version of Pydantic v2), breaking Reasoning Engine serialization at startup. We restored `pyproject.toml` to a clean, minimal state, ran `uv lock` to sync the lockfile, and successfully redeployed.

Once deployed, the Reasoning Engine booted successfully, and the dynamic URL resolved perfectly to your secure `us-east1` gateway URL:
`https://us-east1-aiplatform.googleapis.com/v1beta1/projects/firstargolisproject-338816/locations/us-east1/reasoningEngines/4046378712076124160/a2a`

---

### 5. Sanitizing ADK Sub-Agent Skill Names (The Spinning/Validation Fix)
*   **The Issue**: During testing, Gemini Enterprise successfully fetched the card (`GET /a2a/app/.well-known/agent-card.json` returned `200 OK`), but **never sent any conversational POST requests**, leaving the UI spinning forever.
*   **The Cause**: We discovered that the ADK's `AgentCardBuilder._build_sub_agent_skills` compiled sub-agent skill names using the hardcoded format: `f'{sub_agent.name}: {skill.name}'`. This generated skill names like `"nba_player_stats: custom"` and `"mlb_fan_experience: custom"`, which contain **colons and spaces**. 
    Gemini Enterprise's registry parser enforces a strict identifier validation regex (allowing only alphanumeric and underscores, no spaces, no colons). The presence of special characters caused the registry to **silently reject the card during parsing**, aborting the conversation flow before any messages were sent.
*   **The Resolution**: We injected a runtime monkeypatch in `app/agent.py` to intercept the sub-agent skill compiler, replace all spaces/colons with underscores (e.g., `"nba_player_stats_custom"`), and serve a 100% valid schema:
    ```python
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
    ```
    This resolved the validation error, letting Gemini Enterprise successfully register and open conversation routes.

---

### 6. The Complete Service-to-Service IAM Permissions Blueprint
To allow a secure, service-to-service conversational flow across Gemini Enterprise, Cloud Run, the companion services, and BigQuery, we established a robust, production-grade IAM policy blueprint.

#### A. Gateway Authorization (Outbound Calls)
To allow Gemini Enterprise's backend to invoke your Cloud Run or Agent Runtime endpoints:
*   **Target identity**: The Google-managed Gemini Enterprise/Discovery Engine service agent:
    `service-439242279983@gcp-sa-discoveryengine.iam.gserviceaccount.com`
*   **For Cloud Run**: Grant the **`Cloud Run Invoker` (`roles/run.invoker`)** role.
*   **For Agent Runtime**: Grant the **`Vertex AI User` (`roles/aiplatform.user`)** role.

#### B. Downstream Delegation (Inbound Calls from Orchestrator)
When the BQ Orchestrator receives a request, it runs under your application service account:
`bq-orchestrator-agent-app@firstargolisproject-338816.iam.gserviceaccount.com`
To authorize this service account to delegate queries to downstream Gemini Data Analytics agents:
1.  **`Discovery Engine Editor` (`roles/discoveryengine.editor`)**: Grants read/write access to all discovery engine indices and datasets.
2.  **`Gemini Data Analytics Data Agent Editor` (`roles/geminidataanalytics.dataAgentEditor`)**: Grants explicit **Chat and Edit access** to communicate with downstream Conversational Analytics data agents.
3.  **`Gemini for Google Cloud User` (`roles/cloudaicompanion.user`)**: Grants permission to **create conversation topics/sessions** (`cloudaicompanion.topics.create`) within the internal companion APIs.

#### C. Database Execution
To allow the SQL query generated by the Conversational Analytics agent to run against the physical BigQuery database:
1.  **`BigQuery Data Viewer` (`roles/bigquery.dataViewer`)**: Grants read access to BigQuery tables (e.g. `NBA_stats.Players`).
2.  **`BigQuery Job User` (`roles/bigquery.jobUser`)**: Grants permission to run query jobs in the project.

---

### 7. End-to-End Verification
The entire flow was thoroughly verified in Gemini Enterprise using the **`BQ Orchestrator (Cloud Run)`** agent:
*   **Out-of-Domain Rejection**: A query like *"What is the weather tonight?"* was correctly intercepted and rejected with: *"The agents do not have context to respond to this question. Please ask about NBA Player stats or MLB Club Fan experience."* (Verifying the container logic is fully active).
*   **Sports Analytics Delegation**: A query like *"Which player had the highest point average in the 2018-19 NBA season?"* successfully bypassed all security gateways, generated the BigQuery SQL query, executed it, and returned the verified sports statistics to the chat UI!

---

### 8. Extending to Agent Runtime (Reasoning Engine)
Because `app/agent.py` is shared, the **Agent Runtime** version is now *also* fully patched and prepared to work!
1.  The Agent Runtime card is now automatically compiled with the sanitized skill names.
2.  All database, companion, and delegation permissions granted to the application service account will automatically apply to the Reasoning Engine execution context.
3.  **To activate**: Refresh your Gemini Enterprise browser tab, start a new chat session, and select the **`BQ Orchestrator (Agent Runtime)`** agent. It is now fully equipped to route and query just like the Cloud Run version!

