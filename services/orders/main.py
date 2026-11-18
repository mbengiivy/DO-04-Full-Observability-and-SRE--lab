import logging
import os

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

resource = Resource.create({"service.name": "orders"})

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
        "%(asctime)s %(levelname)s service=orders "
        "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s %(message)s"
    ),
)
logger = logging.getLogger("orders")

# Custom metrics
meter = metrics.get_meter("orders")
orders_created = meter.create_counter(
    "orders.created", description="Orders successfully created"
)
payment_failures = meter.create_counter(
    "orders.payment_failures", description="Failed calls to payments"
)

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://127.0.0.1:8001")
orders = []


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/orders")
def create_order(amount: int):
    logger.info("creating order amount=%s", amount)
    try:
        response = httpx.post(
            f"{PAYMENTS_URL}/charge",
            params={"amount": amount},
            timeout=2.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        payment_failures.add(1)
        logger.error("payment call failed: %s: %r", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="payment failed")

    order = {"id": len(orders) + 1, "amount": amount, "status": "paid"}
    orders.append(order)
    orders_created.add(1)
    logger.info("order created id=%s amount=%s", order["id"], amount)
    return order