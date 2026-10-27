# Keep API

This repository contains the API service for the Keep platform.

## Directory Structure

 The project follows a standard `src` layout.

### `src/keep`

This is the main package containing the application code.

- **`api/`**: The Core FastAPI application.
    - `api.py`: The entry point for the FastAPI app. Sets up middleware, routes, and startup/shutdown events.
    - `routes/`: Implementation of API endpoints, grouped by functionality (e.g., `alerts.py`, `workflows.py`).
    - `core/`: Core API logic, dependencies, and configurations.
- **`actions/`**: Implementation of various actions that can be triggered (e.g., sending notifications, creating tickets).
- **`common/`**: Shared utilities used across the platform.
    - Includes database models (`models.py`), logging setup, utility functions, and possibly shared configuration.
- **`contextmanager/`**: Likely manages execution contexts, possibly for tenant segregation or request contexts.
- **`event_subscriber/`**: logic for subscribing to and processing events (e.g., from Kafka or queues), acting as a consumer.
- **`exceptions/`**: Custom exception classes for the application.
- **`functions/`**: Generic helper functions or business logic helpers.
- **`identitymanager/`**: Authentication and Authorization (RBAC) logic. Handles user identity, roles, and permissions.
- **`iohandler/`**: Handles Input/Output operations, possibly for file processing or external data ingestion.
- **`parser/`**: Logic for parsing rules, alerts, or configuration files (e.g., YAML parsing for workflows).
- **`providers/`**: Integrations with third-party services and tools (e.g., AWS, Datadog, Slack). This is a heavy directory containing the logic to talk to the "outside world".
- **`rulesengine/`**: The logic that evaluates alert rules and conditions.
- **`searchengine/`**: Interaction with search backends (e.g., Elasticsearch or OpenSearch) for querying alerts/events.
- **`secretmanager/`**: Interfaces for retrieving secrets (api keys, credentials) from secret stores (K8s secrets, HashiCorp Vault, etc.).
- **`step/`**: Definitions of workflow steps.
- **`throttles/`**: Logic for rate limiting or throttling actions/alerts to prevent noise.
- **`topologies/`**: Logic related to service topology or dependency mapping.
- **`validation/`**: Schemas and validation logic for data inputs.
- **`workflowmanager/`**: core engine for managing and orchestrating the execution of workflows.

### Root Files
- **`pyproject.toml`**: Python dependency and build configuration.

## Getting Started

1.  **Install Dependencies**:
    ```bash
    pip install poetry
    poetry install
    ```

2.  **Run the API**:
    ```bash
    poetry run uvicorn src.keep.api.api:get_app --reload
    ```
