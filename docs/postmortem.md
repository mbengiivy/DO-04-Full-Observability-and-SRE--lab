# Postmortem: Checkout failures during chaos experiments on `payments`

**Status:** Complete. All timeline entries are confirmed. The Incident 2 root cause remains at moderate confidence because the controlled rerun was not performed (see section 5 and the open items at the end).
**Author:** Ivy
**Environment:** local `kind` cluster (`lab`), namespace `app`
**Time zone:** all times are local time (UTC+3) unless stated. Alertmanager shows UTC and Grafana follows the browser, so values were converted.
**Format:** blameless. This document looks at how the system behaved and what let the failures happen. It does not look at who did what.

---

## 1. Summary

Two controlled failures were injected into the `payments` service with Chaos Mesh to test whether the observability stack lets an on-call engineer detect, locate, and explain a problem using dashboards alone.

| | Incident 1 | Incident 2 |
|---|---|---|
| Date | 13 Nov 2026 | 17 Nov 2026 |
| Fault injected | `PodChaos` `pod-failure`, 5 minutes | `NetworkChaos` `delay` of 600 ms (+/- 50 ms), 6 minutes |
| Expected symptom | Failed checkouts (errors) | Slow but successful checkouts (no errors) |
| Observed symptom | Failed checkouts, as expected | Slow checkouts **and** failed checkouts |
| Alert that fired | `CheckoutAvailabilityFastBurn` (critical) | `CheckoutAvailabilityFastBurn` (critical), then `CheckoutAvailabilitySlowBurn` (warning) |
| Time to detect | about 2 minutes | about 2 minutes (11:08 to 11:10) |
| Impact duration | about 5 minutes | about 6 minutes injected, slower response lingered afterwards |
| Root cause | `payments` unreachable, connection refused | Probable: delay slowed health-check replies past the 1 s probe timeout, so Kubernetes removed and restarted the only `payments` pod. A pre-existing background instability may have contributed |

The main finding is that **Incident 2 did not behave as predicted**. A latency fault turned into an availability fault because of how the Kubernetes health probes were configured. All three signals (metrics, traces, logs) were needed to see this.

---

## 2. System under test

```
user/load --> frontend --> orders --> payments
                              |
                      (OpenTelemetry SDK in each service)
                              v
                  OpenTelemetry Collector
                   |            |            |
              Prometheus      Tempo        (logs: Alloy -> Loki)
                   \            |            /
                              Grafana
                                 |
                  Alertmanager --> email
```

- Three FastAPI services, one replica each, on a single-node `kind` cluster.
- Metrics, traces, and logs instrumented with OpenTelemetry. Log lines carry `trace_id` and `span_id`.
- Health probes on each pod: `/health`, **timeout 1 s**, readiness every 5 s (3 failures), liveness every 10 s (3 failures).

## 3. Service level objectives

| SLO | SLI | Target | Window | Error budget |
|---|---|---|---|---|
| Availability | successful checkouts / all checkouts | 99.9% | 30 days | 0.1% |
| Latency | checkouts under 500 ms / all checkouts | 95% | 30 days | 5% |

The latency target was **chosen from measurement**, not taken from the example in the brief. On a healthy baseline the app served about 87% of checkouts under 300 ms and about 97.5% under 500 ms, so 99.9% under 300 ms would have been unmeetable. The 500 ms / 95% target leaves headroom while still catching real degradation.

Alerts use multi-window burn rates. Fast burn: 1 h and 5 m burn rate above 14.4 (critical). Slow burn: 1 h and 30 m above 6 (warning). A critical alert inhibits the warning for the same SLO.

---

## 4. Incident 1: pod failure (13 Nov 2026)

### Impact
- Checkouts returned HTTP 502 for about 5 minutes.
- Dashboard, measured over the selected range (which included healthy minutes before the fault): availability SLI 75.94%, latency SLI 80.02%.
- Availability burn rate peaked at **631** (5 m window) and **483** (1 h window), against an alert threshold of 14.4. A burn rate of 631 corresponds to about 63% of requests failing, and at that pace the 30-day budget would be exhausted in about 68 minutes.

### Timeline (local time)

| Time | Event |
|---|---|
| 22:37 | Experiment applied (T0). First 502 returned. `CheckoutAvailabilitySlowBurn` pending |
| 22:38 | `CheckoutAvailabilityFastBurn` pending |
| 22:39 | `CheckoutAvailabilityFastBurn` **firing**. Email received |
| 22:37 to 22:42 | Investigation using dashboards (below) |
| 22:42 | Fault ended. Checkouts returned 200 |

### Detection and investigation (metric, then trace, then log)

1. **Metric (that something is wrong).** The `outcome="error"` counter rose while `success` stayed flat. Availability burn rate crossed the threshold. The fast-burn alert fired about 2 minutes after the first failure, mostly the 1-minute `for:` delay plus rule evaluation.
2. **Trace (where).** The red path ended at the `orders` span that calls `payments` (6.42 ms). There was **no `payments` span** in the trace, so the request never reached it. A failure this fast (6 ms) indicated an immediate refusal, not a timeout.
3. **Log (why).** `orders` logged `payment call failed: ConnectError('[Errno 111] Connection refused')`, carrying the trace ID, which linked directly to the trace in Tempo.

### Root cause
`payments` had a single replica, and the injected fault made that pod unavailable. With no other replica to serve traffic, `orders` could not connect and returned 502 to `frontend`.

### Contributing factors
- One replica per service, so no redundancy.
- No retry or fallback in `orders` when `payments` is unreachable.

---

## 5. Incident 2: network latency (17 Nov 2026)

### What we expected
A 600 ms delay is below both service timeouts (2 s and 3 s), so checkouts should have **succeeded, slowly**. We predicted no red spans, no errors, availability at 100%, and only a latency alert, if any.

### What we observed

| Time | Event |
|---|---|
| 11:08 | Experiment applied (T0). p95 checkout latency crossed the 0.5 s SLO line |
| about 11:09 | p95 peaked at about **4.7 s** |
| 11:11:29 to 11:12:10 | A `payments` container ran for 41 s, then was stopped by Kubernetes (exit code 0, liveness probe failure) |
| 11:13:41 | New `payments` container started |
| about 11:14 | Injected fault ended (6 minutes) |
| about 11:15 | p95 fell back to about 0.5 s, then settled between 0.5 s and 1.0 s until at least 11:30 |
| 11:10 | `CheckoutAvailabilityFastBurn` **firing** (critical). Email received. Time to detect: about 2 minutes |
| 11:20 | `CheckoutAvailabilityFastBurn` resolved. `CheckoutAvailabilitySlowBurn` firing (warning). The warning was only sent once the critical alert had cleared, because inhibition no longer applied |
| 11:40 | `CheckoutAvailabilityFastBurn` **firing again, with no experiment active** (the injected fault ended at about 11:14) |
| 11:44 | `CheckoutAvailabilityFastBurn` resolved |
| 11:45 | `CheckoutAvailabilitySlowBurn` resolved |
| 12:01 | `CheckoutAvailabilitySlowBurn` firing again, with no experiment active |
| 12:09 | `CheckoutAvailabilitySlowBurn` resolved |

Direct measurements from the client after the injected window:

- Run 1: ten requests of 0.27 to 0.60 s, with one at **2.00 s**, which is the `orders` to `payments` timeout.
- Run 2: one **502 after 9.65 s**, then nine successful requests of 0.46 to 1.0 s.
- Run 3 (direct `orders` to `payments` health call, three times): 0.41 s, 0.23 s, 0.22 s. A 600 ms delay would add well over half a second to each, so **the injected delay was no longer active**. The values are still much slower than expected for an in-cluster call (normally tens of milliseconds).
- Run 4 (ten checkouts, later): a first request of 3.72 s, nine others of 0.13 to 0.55 s, and one **502 after 3.17 s**, which matches the 3 s timeout in `frontend`. This occurred with no fault injected.

For reference, the healthy baseline was about 0.1 to 0.3 s.

### Investigation
- **Metrics:** latency p95 and latency burn rate rose as expected, **and availability alerts fired**, which was not expected.
- **Traces and logs:** looking at individual slow requests showed no red spans and no errors. Looking across the whole window showed `Connection refused` errors from `orders`, which indicates `payments` dropped out of service partway through.
- **Kubernetes:** `kubectl describe` on the `payments` pod showed `Readiness probe failed ... context deadline exceeded`, `Readiness probe failed ... connection refused`, `Liveness probe failed`, and `Container payments failed liveness probe, will be restarted`. Probe timeout was 1 s.

### Root cause (probable)
The network delay applies to every packet leaving the `payments` pod, **including replies to health-check requests**. Establishing the connection and returning the response each took roughly 600 ms, which exceeds the 1 s probe timeout. Consequences:

1. Readiness probes failed, so Kubernetes removed `payments` from the Service. `orders` then received connection refused, producing 502s (availability fault).
2. Liveness probes failed, so Kubernetes restarted the container. The 41 s container lifetime matches the liveness settings (10 s initial delay plus three failures at 10 s intervals, about 40 s).

So a latency fault became an availability fault.

**Confidence:** moderate. The probe events, container timestamps, alert types, error text, and the timing (p95 peak and the first alert within 2 minutes of T0) are consistent with this explanation. However, the pods were already restarting on their own about every 45 minutes, and alerts fired again at 11:40 and 12:01 with no fault active. So the chaos-induced restart cannot be separated from the background instability without a controlled rerun, which was not done. See action item 1.

### Contributing factors
- Probe timeout of 1 s is too tight for a delayed or busy Python service.
- Single replica, so removing it from the Service means a complete outage.
- Liveness and readiness probes share one endpoint and the same timeout.
- Evidence the baseline was not clean: before this experiment the pods already showed **139 to 165 restarts over about 5 days** (roughly one every 45 minutes), with probe failures logged throughout. This probably explains alerts that fired before any experiment was applied.
- The p95 settled at 0.5 to 1.0 s after the fault ended. Direct timing showed the injected delay was gone (0.22 to 0.41 s), so the slowness came from elsewhere. Likely candidates, none verified, are CPU throttling at the 300m limit, load on the laptop (the cluster shares 8 GB with the monitoring stack), and slow DNS.
- **Instability continued after the experiment.** Further alerts fired at 11:40 and 12:01, and intermittent multi-second stalls and a 502 (3.17 s) occurred with no fault injected. This is the strongest sign that the baseline itself is unstable and that background probe-driven restarts affect results.

---

## 6. What went well

- The pipeline worked end to end: metrics, traces, and logs all captured both incidents, and Grafana links moved between logs and traces in one click.
- Alerts fired, routed by severity, and reached email. Inhibition behaved as designed.
- Incident 1 was diagnosed from dashboards alone, in the order metric, trace, log.
- Incident 2 showed that the observability stack can reveal when an assumption is wrong. The expected signature (slow but green) differed from what happened, and the data showed why.

## 7. What went wrong

- The experiment behaved differently from the prediction, because the effect on health probes was not considered when the fault was designed.
- The baseline was not clean (frequent restarts and intermittent stalls), which made it hard to separate experiment effects from background noise. In particular, alerts continued to fire after the experiment ended, which would have been confusing without the earlier baseline check.
- Alerts fired during setup, before any fault, because of small samples and a freshly started Prometheus.

## 8. Where we got lucky
- The pod restarts and probe events were still available when examined. With no persistent storage on Prometheus, Loki, or Tempo, a restart of those pods would have erased the evidence.

---

## 9. Limitations of this lab

- **30-day SLO window with minutes of data.** The budget panels compute over the dashboard time range, so values such as -23,956% demonstrate the mechanism and are not realistic.
- **Burn-rate windows on fresh data.** After a Prometheus restart the 1 h window holds only minutes of data, so it behaves like a short window. In a long-running system a 6-minute disturbance would likely not trip the fast-burn alert.
- **Low traffic.** About one request per second means the ratios swing widely on small samples. A minimum-traffic condition on alerts would reduce false alarms.
- **No persistence** for Prometheus, Loki, Tempo, and Grafana. Evidence has to be captured (screenshots, exported queries) as it happens.
- **Single node, single replica per service.**

---

## 10. Action items

| # | Action | Type | Priority | Status |
|---|---|---|---|---|
| 1 | Raise probe timeouts to 3 to 5 s, then **rerun the latency experiment** to confirm it produces slow-but-successful checkouts with no restarts | Prevent / verify | High | Not started |
| 2 | Run at least 2 replicas of `payments` (and the other services) so removing one pod is not an outage | Prevent | High | Not started |
| 3 | Separate the liveness check from the readiness check, with a more forgiving liveness probe, and add a `startupProbe` | Prevent | Medium | Not started |
| 4 | Add alerts on pod restarts and failed probes, so background restarts are visible | Detect | Medium | Not started |
| 5 | Investigate the roughly 45-minute background restart cycle and intermittent stalls (resource limits, probe timeouts, machine load, DNS) | Investigate | **High** | Not started |
| 6 | Add a minimum-traffic condition to the burn-rate alerts | Detect | Low | Not started |
| 7 | Add a retry with backoff, and a clearer error, in `orders` when `payments` is unreachable | Mitigate | Low | Not started |
| 8 | Enable persistent storage (or export dashboards and queries) for Prometheus, Loki, Tempo, and Grafana | Process | Low | Not started |
| 9 | Re-measure SLO targets after fixes and tighten them if the system allows | Process | Low | Not started |

## 11. Lessons learned

1. **Metric first, then trace, then log.** The first signal is usually a metric. Traces say where, and logs say why.
2. **The deepest red span is where a failure started. The longest span is usually just the root.** For slowness, look for the span with unexplained self time.
3. **A fault is rarely isolated.** Injecting latency affected health checks, which removed a pod from service. Design experiments with the whole system in mind, including probes and timeouts.
4. **A missing span is evidence.** In Incident 1 the absence of a `payments` span showed the request never reached it.
5. **Set SLOs from measurement.** The brief's example target would have failed on a healthy system.
6. **Check your own baseline.** Background restarts made early results look worse than the experiments caused.

---

## Appendix: evidence

Screenshots to include (replace the placeholders with file paths in `docs/evidence/`):

- Incident 1: dashboard during outage, Prometheus `/alerts` firing, Tempo trace with red spans, Loki `Connection refused` lines, alert email
- Incident 2: p95 latency graph over 11:00 to 11:30, alert emails (firing, resolved, firing), `kubectl describe pod` events for `payments`, direct latency measurements

## Open items

- Confirmed: the `payments-latency` experiment object has been deleted. Direct timing also showed the injected delay was gone.
- Controlled rerun with longer probe timeouts (action item 1) was **not performed**, so the Incident 2 root cause stays at moderate confidence.
