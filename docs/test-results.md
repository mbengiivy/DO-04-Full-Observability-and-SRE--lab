# Test results

All tests were run on a single-node `kind` cluster. Times are local (UTC+3). Screenshots are in `docs/evidence/`.

## 1. Component tests

| Component | Test | Result |
|---|---|---|
| Prometheus | `up` | `1` for `job="prometheus"`, so scraping works |
| Prometheus | `sum by (code) (rate(prometheus_http_requests_total[5m]))` | Three series, `200` at about 0.3 req/s and the others at 0 |
| Prometheus | `sum by (outcome) (checkouts_total)` after sending traffic | `success` counter present and matching requests sent |
| Collector to Prometheus | Target `otel-collector` on the Targets page | `UP`, application metrics queryable |
| Loki | `/loki/api/v1/label/service/values` | `frontend`, `orders`, `payments` |
| Tempo | Fetched a trace by `trace_id` taken from a frontend log line | One trace containing spans from all three services |
| Trace structure | Parent and child span IDs across services | Chain `frontend` to `orders` to `payments`, matching `parentSpanId` values |
| Healthy trace | Total duration of one checkout | 178 ms (payments 54 ms, orders 112 ms) |
| Grafana | Loki log line, "Open trace in Tempo" | Opens the matching trace |
| Grafana | Tempo span, LOG button | Opens the matching Loki logs |
| Alertmanager | Always-firing test rule | Email delivered after fixing the From address and app password |

## 2. SLI baseline (clean run, steady traffic)

| SLI | Result |
|---|---|
| Availability | 1 (100%) |
| Latency under 300 ms | about 0.86 to 0.89 |
| Latency under 500 ms | about 0.97 to 0.98 |
| Burn rate | 0 |

A first measurement taken with failures still inside the window gave latency under 500 ms of 0.94 and a burn rate of 16.7, which showed that failures are counted in the latency histogram. The clean run was repeated. The SLO targets were chosen from the clean numbers.

## 3. Practice incident (scale `payments` to 0)

| Step | Signal | Observation |
|---|---|---|
| Metric | `checkouts_total{outcome="error"}` | rose to about 30, success flat |
| Trace | red spans | ended at the `orders` call to `payments`, no `payments` span |
| Log | Loki | `Connection refused` |

## 4. Chaos experiment 1: pod failure (13 Nov, 5 minutes)

| Measure | Result |
|---|---|
| T0 | 22:37 (first 502 at 22:37) |
| `CheckoutAvailabilityFastBurn` pending / firing | 22:38 / 22:39 (email received) |
| Recovery | 22:42 |
| Peak availability burn rate | 631 (5 m), 483 (1 h) |
| Dashboard over selected range | availability SLI 75.94%, latency SLI 80.02% |
| Deepest red span | `orders` call to `payments`, 6.42 ms, no `payments` span |
| Log | `payment call failed: ConnectError('[Errno 111] Connection refused')` |

## 5. Chaos experiment 2: 600 ms latency (17 Nov, 6 minutes)

| Measure | Result |
|---|---|
| T0 | 11:08. p95 crossed the 0.5 s SLO line at 11:08 and peaked at about 4.7 s around 11:09 |
| Availability fast burn | firing 11:10, resolved 11:20 |
| Availability slow burn | firing 11:20 (after the critical alert cleared), resolved 11:45 |
| Later alerts with no fault active | fast burn 11:40 to 11:44, slow burn 12:01 to 12:09 |
| Pod events | readiness and liveness probe failures, `payments` container restarted (41 s lifetime) |
| Direct latency, orders to payments, after the fault | 0.41, 0.23, 0.22 s (delay no longer active) |
| Direct checkout latency, later run | 3.72 s first request, then 0.13 to 0.55 s, and one 502 after 3.17 s |

Finding: the latency fault produced availability alerts because the delay also affected health probes. See the [postmortem](postmortem.md) for the analysis, confidence level, and action items.

## 6. Not done

- No dedicated load test (a tool such as k6) was run. The numbers above come from a roughly one request per second traffic loop.
- The controlled rerun of experiment 2 with longer probe timeouts was not performed.
