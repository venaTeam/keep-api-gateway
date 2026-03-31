import abc
import json
import logging
from typing import Optional
import ssl

from aiokafka import AIOKafkaProducer
from arq import ArqRedis
from aiokafka.errors import KafkaConnectionError

from keep.common.consts import KEEP_ARQ_QUEUE_BASIC
from keep.common.core.config import config

logger = logging.getLogger(__name__)


def _parse_bootstrap_servers(value: str)-> list[str]:

    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
        return [str(parsed)]
    except json.JSONDecodeError:
        return [s.strip() for s in value.split(",") if s.strip()]


def _create_ssl_context(security_protocol: str, cafile: Optional[str], certfile: Optional[str], keyfile: Optional[str])-> Optional[ssl.SSLContext]:
    
    if security_protocol not in ["SSL", "SASL_SSL"]:
        return None

    ssl_context = ssl.create_default_context(cafile=cafile)
    if certfile and keyfile:
        ssl_context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ssl_context


class EventProducer(abc.ABC):
    @abc.abstractmethod
    async def produce(self, event: dict, **kwargs):
        pass


class RedisEventProducer(EventProducer):
    def __init__(self, arq_pool: ArqRedis):
        self.arq_pool = arq_pool

    async def produce(self, event: dict, **kwargs):
        trace_id = kwargs.get("trace_id")
        logger.info(f"Producing event to Redis ARQ: {trace_id}")
        # Extract arguments expected by the worker
        tenant_id = kwargs.get("tenant_id")
        provider_type = kwargs.get("provider_type")
        provider_id = kwargs.get("provider_id")
        fingerprint = kwargs.get("fingerprint")
        api_key_name = kwargs.get("api_key_name")
        provider_name = kwargs.get("provider_name")

        # Enqueue job matching the signature in alerts.py
        job = await self.arq_pool.enqueue_job(
            "process_event_in_worker",
            tenant_id,
            provider_type,
            provider_id,
            fingerprint,
            api_key_name,
            trace_id,
            event,
            _queue_name=KEEP_ARQ_QUEUE_BASIC,
            provider_name=provider_name,
        )
        logger.info(f"Successfully produced event to Redis ARQ: {trace_id}")
        return job.job_id


class KafkaEventProducer(EventProducer):
    def __init__(self):
        self._started = False


        bootstrap_servers = config("KAFKA_BOOTSTRAP_SERVERS", default="localhost:9092")
        self.bootstrap_servers = _parse_bootstrap_servers(bootstrap_servers)
        self.topic = config("KAFKA_TOPIC", default="keep-events")
        self.max_retries = int(config("KAFKA_MAX_RETRIES", default="3"))

        # DLQ config
        dlq_bootstrap_servers_str = config("KAFKA_DLQ_BOOTSTRAP_SERVERS", default=bootstrap_servers)
        self.dlq_topic = config("KAFKA_DLQ_TOPIC", default="keep-events-dlq")
        self.dlq_bootstrap_servers = _parse_bootstrap_servers(dlq_bootstrap_servers_str)

        # SASL config
        self.security_protocol = config("KAFKA_SECURITY_PROTOCOL", default="PLAINTEXT")
        self.sasl_mechanism = config("KAFKA_SASL_MECHANISM", default="PLAIN")
        self.sasl_plain_username = config("KAFKA_SASL_USERNAME", default=None)
        self.sasl_plain_password = config("KAFKA_SASL_PASSWORD", default=None)
        self.ssl_cafile = config("KAFKA_SSL_CAFILE", default=None)
        self.ssl_certfile = config("KAFKA_SSL_CERTFILE", default=None)
        self.ssl_keyfile = config("KAFKA_SSL_KEYFILE", default=None)


        self.ssl_context = _create_ssl_context(
            self.security_protocol,
            self.ssl_cafile,
            self.ssl_certfile,
            self.ssl_keyfile
        )


        self.producer= self._create_producer(self.bootstrap_servers)
        self.dlq_producer = self._create_producer(self.dlq_bootstrap_servers, is_dlq=True)

    def _create_producer(self, bootstrap_servers: list[str], is_dlq: bool=False)-> AIOKafkaProducer:
        if is_dlq:
            username= config("KAFKA_DLQ_SASL_USERNAME", default=self.sasl_plain_username)
            password = config("KAFKA_DLQ_SASL_PASSWORD", default=self.sasl_plain_password)
        else:
            username = self.sasl_plain_username
            password = self.sasl_plain_password
    
        return AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers,
            security_protocol=self.security_protocol,
            sasl_mechanism=self.sasl_mechanism,
            sasl_plain_username=username,
            sasl_plain_password=password,
            ssl_context=self.ssl_context,
            api_version="auto",
        )

    async def _ensure_started(self):
        if not self._started:
            await self.producer.start()
            await self.dlq_producer.start()
            self._started = True

    async def _send_to_dlq(self, value: bytes, trace_id: str)-> str:
        try:
            await self.dlq_producer.start()
        except RuntimeError:
            pass

        await self.dlq_producer.send_and_wait(self.dlq_topic, value)
        logger.info(f"Successfully produced event to DLQ topic {self.dlq_topic}: {trace_id}")
        return "kafka-async-task-dlq"


    def _build_payload(self, event: dict, **kwargs)-> dict:

        return {
            "event": event,
            "tenant_id": kwargs.get("tenant_id"),
            "provider_type": kwargs.get("provider_type"),
            "provider_id": kwargs.get("provider_id"),
            "fingerprint": kwargs.get("fingerprint"),
            "api_key_name": kwargs.get("api_key_name"),
            "trace_id": kwargs.get("trace_id", "unknown"),
            "provider_name": kwargs.get("provider_name"),
        }
        
    def _serialize_payload(self, payload: dict)-> bytes:
        try:
            # Serialize payload, handling Pydantic models (like AlertDto) and other objects
            return json.dumps(
                payload, default=lambda o: o.dict() if hasattr(o, "dict") else str(o)
            ).encode("utf-8")
        except Exception:
            logger.exception("Failed to serialize event payload")
            raise
    
    async def _produce_with_retry(self, value:bytes, trace_id:str)-> str:
            
        for attempt in range(self.max_retries):
            try:
                await self.producer.send_and_wait(self.topic, value)
                logger.info(f"Successfully produced event to Kafka topic {self.topic}: {trace_id}")
                return "kafka-async-task"
            except Exception as e:
                logger.warning(f"Failed to produce to Kafka main topic {self.topic} (attempt {attempt+1}/{self.max_retries}): {e}")
        return None

    async def produce(self, event: dict, **kwargs) -> str:
        trace_id = kwargs.get("trace_id", "unknown")
        payload = self._build_payload(event, **kwargs)
        value = self._serialize_payload(payload)

        try:
            await self._ensure_started()
        except KafkaConnectionError as e:
            logger.warning(f"Failed to connect to kafka: {e}. Sending directly to DLQ")
            return await self._send_to_dlq_or_raise(value, trace_id)
        
        result = await self._produce_with_retry(value, trace_id)
        if result:
            return result
        
        logger.warning(f"All {self.max_retries} attempts to main topic {self.topic} failed. Sending to DLQ")
        return await self._send_to_dlq_or_raise(value, trace_id)

    async def _send_to_dlq_or_raise(self, value: bytes, trace_id: str)-> str:
        try:
            return await self._send_to_dlq(value, trace_id)
        except Exception:
            logger.exception("Failed to produce event to DLQ")
            raise

    async def close(self):
        if self._started:
            await self.producer.stop()
            await self.dlq_producer.stop()
            self._started = False
