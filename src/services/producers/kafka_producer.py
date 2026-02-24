import json
import logging
from aiokafka import AIOKafkaProducer
from src.config.core import config
from src.services.producers.base_event_handler import EventProducer, EventType

class KafkaEventProducer(EventProducer):
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        bootstrap_servers = config(
            "KAFKA_BOOTSTRAP_SERVERS", default="localhost:9092"
        )
        try:
            self.bootstrap_servers = json.loads(bootstrap_servers)
            if not isinstance(self.bootstrap_servers, list):
                self.bootstrap_servers = str(self.bootstrap_servers).split(",")
        except json.JSONDecodeError:
            self.bootstrap_servers = bootstrap_servers.split(",")
        
        self.topic = config("KAFKA_TOPIC", default="keep-events")

        # SASL config
        self.security_protocol = config("KAFKA_SECURITY_PROTOCOL", default="PLAINTEXT")
        self.sasl_mechanism = config("KAFKA_SASL_MECHANISM", default="PLAIN")
        self.sasl_plain_username = config("KAFKA_SASL_USERNAME", default=None)
        self.sasl_plain_password = config("KAFKA_SASL_PASSWORD", default=None)

        # SSL config
        self.ssl_cafile = config("KAFKA_SSL_CAFILE", default=None)
        self.ssl_certfile = config("KAFKA_SSL_CERTFILE", default=None)
        self.ssl_keyfile = config("KAFKA_SSL_KEYFILE", default=None)

        ssl_context = None
        if self.security_protocol in ["SSL", "SASL_SSL"]:
            import ssl
            ssl_context = ssl.create_default_context(cafile=self.ssl_cafile)
            if self.ssl_certfile and self.ssl_keyfile:
                ssl_context.load_cert_chain(
                    certfile=self.ssl_certfile, keyfile=self.ssl_keyfile
                )
            # If user didn't provide CA file, we rely on system CAs or strict verification off?
            # Typically for self-signed or internal CAs, user provides cafile.
            # We don't force check_hostname=False unless requested, but standard is often strict.
            if not self.ssl_cafile and not self.ssl_certfile:
                # Fallback or specific logic if needed. For now standard default context.
                pass

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            security_protocol=self.security_protocol,
            sasl_mechanism=self.sasl_mechanism,
            sasl_plain_username=self.sasl_plain_username,
            sasl_plain_password=self.sasl_plain_password,
            ssl_context=ssl_context,
            api_version="auto",
        )
        self._started = False

    async def _ensure_started(self):
        if not self._started:
            await self.producer.start()
            self._started = True

    async def produce(self, event: dict, event_type: EventType = EventType.ALERT, **kwargs):
        trace_id = kwargs.get("trace_id")
        self.logger.info(f"Producing event to Kafka: {trace_id}")
        await self._ensure_started()

        # Enrich event with metadata that ARQ passed as args
        # We put everything in the payload for Kafka
        payload = {
            "event": event,
            "event_type": event_type.value if hasattr(event_type, "value") else event_type,
            "tenant_id": kwargs.get("tenant_id"),
            "provider_type": kwargs.get("provider_type"),
            "provider_id": kwargs.get("provider_id"),
            "fingerprint": kwargs.get("fingerprint"),
            "api_key_name": kwargs.get("api_key_name"),
            "trace_id": trace_id,
            "provider_name": kwargs.get("provider_name"),
        }

        try:
            # Serialize payload, handling Pydantic models (like AlertDto) and other objects
            val = json.dumps(
                payload, default=lambda o: o.dict() if hasattr(o, "dict") else str(o)
            ).encode("utf-8")
            await self.producer.send_and_wait(self.topic, val)
            self.logger.info(f"Successfully produced event to Kafka: {trace_id}")
            return "kafka-async-task"
        except Exception as e:
            self.logger.exception("Failed to produce event to Kafka")
            raise e

    async def close(self):
        if self._started:
            await self.producer.stop()
            self._started = False
