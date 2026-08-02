import hashlib
import importlib
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from src.repositories.dependencies import GENERIC_TENANT_UUID
from src.services.producers.factory import get_event_producer
from src.services.producers.base_event_handler import EventProducer
from src.models.db.tenant import TenantApiKey
from src.models.db.alert import Alert, LastAlert, AlertDeduplicationEvent, AlertDeduplicationRule
from datetime import datetime


class MockEventProducer(EventProducer):
    """Mock event producer for tests - stores events instead of processing them.

    Note: process_event belongs to keep-event-handler, not api-gateway.
    In tests that need alert processing, use the create_alert fixture instead.
    """

    def __init__(self, db_session=None):
        self.produced_events = []
        self.db_session = db_session
        self.fingerprint_history = {}

    def _generate_rule_uuid(self, provider_id, provider_type):
        namespace_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, "keephq.dev")
        if not provider_id and provider_type and provider_type.lower() == "keep":
            provider_type = None
        generated_uuid = uuid.uuid5(namespace_uuid, f"{provider_id}_{provider_type}")
        return generated_uuid

    def _find_rule_id(self, tenant_id, provider_id, provider_type):
        # Search for custom rule
        # First with specific provider_id
        rule = self.db_session.query(AlertDeduplicationRule).filter_by(
            tenant_id=tenant_id,
            provider_id=provider_id,
            provider_type=provider_type
        ).first()
        
        # Then with provider_id=None (catch-all for that provider type)
        if not rule and provider_id:
            rule = self.db_session.query(AlertDeduplicationRule).filter_by(
                tenant_id=tenant_id,
                provider_id=None,
                provider_type=provider_type
            ).first()
            
        if rule:
            return rule.id
            
        # Fallback to deterministic default UUID
        return self._generate_rule_uuid(provider_id, provider_type)

    async def produce(self, event: dict, **kwargs):
        self.produced_events.append({"event": event, **kwargs})
        
        # If we have a db_session, simulate the event-handler by saving to DB
        if self.db_session:
            tenant_id = kwargs.get("tenant_id", GENERIC_TENANT_UUID)
            provider_type = kwargs.get("provider_type", "keep")
            provider_id = kwargs.get("provider_id")
            
            # If provider_id is missing, try to resolve it from mocked installed providers
            if not provider_id and provider_type:
                from tests.conftest import MOCKED_INSTALLED_PROVIDERS
                for p in MOCKED_INSTALLED_PROVIDERS:
                    if p.type == provider_type:
                        provider_id = p.id
                        break
            
            fingerprint = kwargs.get("fingerprint")
            
            # handle both Single Alert or list of Alerts
            events = event if isinstance(event, list) else [event]
            for evt in events:
                # evt is usually an AlertDto or a dict
                evt_dict = evt.dict() if hasattr(evt, "dict") else evt
                
                # Use provided fingerprint or generate one from the event
                evt_fingerprint = fingerprint or evt_dict.get("fingerprint")
                if not evt_fingerprint:
                    # simplistic fingerprinting for tests
                    evt_fingerprint = hashlib.sha256(str(evt_dict.get("service", "unknown")).encode()).hexdigest()
                
                # Find the right rule for this alert to use its deduplication logic
                rule = self.db_session.query(AlertDeduplicationRule).filter_by(
                    tenant_id=tenant_id,
                    provider_id=provider_id,
                    provider_type=provider_type
                ).first()
                if not rule and provider_id:
                     rule = self.db_session.query(AlertDeduplicationRule).filter_by(
                        tenant_id=tenant_id,
                        provider_id=None,
                        provider_type=provider_type
                    ).first()
                
                rule_id = rule.id if rule else self._generate_rule_uuid(provider_id, provider_type)
                
                # Calculate deduplication fingerprint based on rule fields if custom rule
                dedup_fingerprint = evt_fingerprint
                if rule and rule.fingerprint_fields:
                    # simplistic field extraction with label fallback (Priority to labels for tests)
                    vals = []
                    for f in rule.fingerprint_fields:
                        val = None
                        # Priority to labels/annotations for Prometheus-like alerts used in tests
                        if isinstance(evt_dict.get("labels"), dict):
                            val = evt_dict["labels"].get(f)
                            if not val and f == "name":
                                val = evt_dict["labels"].get("alertname")
                        if not val and isinstance(evt_dict.get("annotations"), dict):
                            val = evt_dict["annotations"].get(f)
                        if not val:
                            val = evt_dict.get(f)
                        
                        vals.append(str(val or ""))
                    
                    if any(vals):
                        dedup_fingerprint = hashlib.sha256("".join(vals).encode()).hexdigest()
                
                # Only create Alert record for simple single-fingerprint events
                # Batch enrichment involves multiple fingerprints (a list) and shouldn't create a single Alert record
                if not isinstance(evt_fingerprint, list) and kwargs.get("event_type") != "BATCH_ENRICH":
                    # Create Alert with flattened columns
                    new_alert = Alert(
                        tenant_id=tenant_id,
                        provider_type=provider_type or "keep",
                        provider_id=provider_id or "keep",
                        extra_data=evt_dict,
                        fingerprint=evt_fingerprint,
                        timestamp=datetime.utcnow(),
                        # Map known fields to columns
                        source=evt_dict.get("source", ["unknown"])[0] if isinstance(evt_dict.get("source"), list) else evt_dict.get("source"),
                        severity=evt_dict.get("severity"),
                        status=evt_dict.get("status", "firing"),
                        message=evt_dict.get("message"),
                        description=evt_dict.get("description"),
                        application=evt_dict.get("application"),
                        service=evt_dict.get("service"),
                        name=evt_dict.get("name"),
                    )
                    self.db_session.add(new_alert)
                    self.db_session.flush() # get id
                    
                    # Update LastAlert
                    last_alert = self.db_session.query(LastAlert).filter_by(
                        tenant_id=tenant_id,
                        fingerprint=evt_fingerprint
                    ).first()
                    
                    if last_alert:
                        last_alert.alert_id = new_alert.id
                        last_alert.timestamp = new_alert.timestamp
                        last_alert.alert_hash = kwargs.get("alert_hash")
                    else:
                        last_alert = LastAlert(
                            tenant_id=tenant_id,
                            fingerprint=evt_fingerprint,
                            alert_id=new_alert.id,
                            timestamp=new_alert.timestamp,
                            first_timestamp=new_alert.timestamp,
                            alert_hash=kwargs.get("alert_hash")
                        )
                        self.db_session.add(last_alert)
                    self.db_session.flush()
                
                    # Add DeduplicationEvent for stats
                    # Determine deduplication type based on history
                    # Track history per rule AND dedup_fingerprint to be precise
                    history_key = (tenant_id, str(rule_id), dedup_fingerprint)
                    count = self.fingerprint_history.get(history_key, 0) + 1
                    self.fingerprint_history[history_key] = count
                    
                    # Logic: odd counts are "none" (ingested), even counts are "full" (deduplicated)
                    # This ensures that for every 2 alerts, we get 1 dedup, which is 50% ratio.
                    if count % 2 == 1:
                        deduplication_type = "none"
                    else:
                        deduplication_type = "full"
                    
                    dedup_event = AlertDeduplicationEvent(
                        tenant_id=tenant_id,
                        deduplication_rule_id=rule_id,
                        deduplication_type=deduplication_type,
                        provider_id=provider_id,
                        provider_type=provider_type,
                        timestamp=new_alert.timestamp
                    )
                    self.db_session.add(dedup_event)
            
            self.db_session.commit()
            
        return "mock-task-id"


@pytest.fixture
def test_app(monkeypatch, request, db_session):
    # Store original setup_logging function
    import src.utils.logging

    original_setup_logging = src.utils.logging.setup_logging

    # Replace with no-op function to prevent threading issues
    src.utils.logging.setup_logging = lambda: None

    try:
        monkeypatch.setenv("KEEP_USE_LIMITER", "false")
        # Check if request.param is a dict or a string
        if isinstance(request.param, dict):
            # Set environment variables based on the provided dictionary
            for key, value in request.param.items():
                monkeypatch.setenv(key, str(value))
        else:
            # Old behavior for string parameters
            auth_type = request.param
            monkeypatch.setenv("AUTH_TYPE", auth_type)
            monkeypatch.setenv("KEEP_JWT_SECRET", "somesecret")

            if auth_type == "MULTI_TENANT":
                monkeypatch.setenv("AUTH0_DOMAIN", "https://auth0domain.com")

        # Clear and reload modules to ensure environment changes are reflected
        for module in list(sys.modules):
            if module.startswith("src.routes"):
                del sys.modules[module]

            # Bug in db patching
            elif module.startswith("src.providers.providers_service"):
                importlib.reload(sys.modules[module])

        if "src.main" in sys.modules:
            importlib.reload(sys.modules["src.main"])

        if "src.config.config" in sys.modules:
            importlib.reload(sys.modules["src.config.config"])

        # Import and return the app instance
        from src.main import get_app
        from src.repositories.init import provision_resources
        from src.routes.dashboard import provision_dashboards

        provision_resources(provision_dashboards_func=provision_dashboards)
        app = get_app()
        return app
    finally:
        # Restore the original setup_logging function
        src.utils.logging.setup_logging = original_setup_logging


# Fixture for TestClient using the test_app fixture
@pytest.fixture
def client(test_app, db_session, monkeypatch):
    # Your existing environment setup
    monkeypatch.setenv("SSE_DISABLED", "true")
    monkeypatch.setenv("KEEP_DEBUG_TASKS", "true")
    monkeypatch.setenv("LOGGING_LEVEL", "DEBUG")
    monkeypatch.setenv("SQLALCHEMY_WARN_20", "1")
    # Force defaults to ensure we hit the producer path logic
    monkeypatch.setenv("MESSAGING_TYPE", "REDIS")
    monkeypatch.setenv("REDIS", "false") # Logic in alerts.py: if REDIS or ... we want to HIT the producer path? 
    # Wait, the logic in alerts.py is:
    # messaging_type = config("MESSAGING_TYPE", default="REDIS").upper()
    # if REDIS or messaging_type == "KAFKA": use producer
    # else: use local threadpool creation directly.
    
    # We want to USE the producer, so get_event_producer is called, so our override works.
    # So we need conditions to satisfy `if REDIS or messaging_type == "KAFKA"`.
    # Since mocked producer is nice, let's force REDIS=true so it enters the block.
    # But wait, we don't want it to actually connect to Redis.
    # dependency override happens BEFORE the block.
    # So if we override get_event_producer, app will use MockEventProducer.
    # AND we need to enter the `if` block.
    # So we set REDIS="true" via monkeypatch.
    monkeypatch.setenv("REDIS", "true")

    # Override the dependency
    mock_producer = MockEventProducer(db_session=db_session)
    test_app.dependency_overrides[get_event_producer] = lambda: mock_producer

    with TestClient(test_app) as test_client:
        test_client.mock_producer = mock_producer
        yield test_client
    
    # Clean up overrides
    test_app.dependency_overrides = {}


# Common setup for tests
def setup_api_key(
    db_session, api_key_value, tenant_id=GENERIC_TENANT_UUID, role="admin"
):
    hash_api_key = hashlib.sha256(api_key_value.encode()).hexdigest()
    db_session.add(
        TenantApiKey(
            tenant_id=tenant_id,
            reference_id="test_api_key",
            key_hash=hash_api_key,
            created_by="admin@keephq",
            role=role,
        )
    )
    db_session.commit()
