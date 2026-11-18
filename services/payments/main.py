import logging
import random
import time

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# One resource shared by traces and metrics, so both carry the same service name
resource = Resource.create({"service.name": "payments"})

# Traces
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)

# Metrics (snapshotted and sent to the Collector every 10 seconds)
# OTLPMetricExporter reads OTEL_EXPORTER_OTLP_ENDPOINT (default http://localhost:4318)
reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(), export_interval_millis=10000
)
metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

# Logs, stamped with the active trace and span IDs
LoggingInstrumentor().instrument(
    set_logging_format=True,
    log_level=logging.INFO,
    logging_format=(
        "%(asctime)s %(levelname)s service=payments "
        "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s %(message)s"
    ),
)
logger = logging.getLogger("payments")

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/charge")
def charge(amount: int):
    # Fake the time a real card charge takes: 50 to 200 ms
    time.sleep(random.uniform(0.05, 0.2))
    logger.info("charged amount=%s", amount)
    return {"status": "charged", "amount": amount}