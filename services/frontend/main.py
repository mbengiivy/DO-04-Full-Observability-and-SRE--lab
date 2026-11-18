import logging
import os
import time

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

resource = Resource.create({"service.name": "frontend"})

# Traces
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)

# Metrics
# OTLPMetricExporter reads OTEL_EXPORTER_OTLP_ENDPOINT (default http://localhost:4318)
reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(), export_interval_millis=10000
)
metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

# Logs
LoggingInstrumentor().instrument(
    set_logging_format=True,
    log_level=logging.INFO,
    logging_format=(
        "%(asctime)s %(levelname)s service=frontend "
        "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s %(message)s"
    ),
)
logger = logging.getLogger("frontend")

# Custom metrics: these two are the basis of your SLIs
meter = metrics.get_meter("frontend")
checkouts = meter.create_counter(
    "checkouts", description="Checkout attempts by outcome"
)
checkout_duration = meter.create_histogram(
    "checkout.duration",
    unit="s",
    description="Checkout latency",
    # 0.3 is deliberate: it lets us measure the "under 300 ms" SLO exactly
    explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5],
)

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

ORDERS_URL = os.getenv("ORDERS_URL", "http://127.0.0.1:8002")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/checkout")
def checkout(amount: int = 2500):
    logger.info("checkout started amount=%s", amount)
    start = time.perf_counter()
    outcome = "success"
    try:
        response = httpx.post(
            f"{ORDERS_URL}/orders",
            params={"amount": amount},
            timeout=3.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        outcome = "error"
        logger.error("checkout failed: %s: %r", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="checkout failed")
    finally:
        # Runs on success and failure, so every attempt is counted exactly once
        checkouts.add(1, {"outcome": outcome})
        checkout_duration.record(time.perf_counter() - start)

    logger.info("checkout completed")
    return {"message": "Thank you for your order", "order": response.json()}