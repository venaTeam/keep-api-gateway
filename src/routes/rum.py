from fastapi import APIRouter
from pydantic import BaseModel

# Via prometheus_multiproc, not prometheus_client: this module defines metrics,
# so it is subject to the multiprocess-dir ordering (see that module's docstring).
from src.repositories.prometheus_multiproc import REGISTRY, Histogram

def get_or_create_histogram(name, documentation, labelnames, buckets):
    try:
        return Histogram(name, documentation, labelnames, buckets=buckets)
    except ValueError:
        if name in REGISTRY._names_to_collectors:
            return REGISTRY._names_to_collectors[name]
        raise

router = APIRouter()

# Define Histograms for Web Vitals
# Buckets are customized for Web Vitals thresholds (in milliseconds/seconds as appropriate)
# CLS: 0.1 (good), 0.25 (needs improvement)
CLS_HISTOGRAM = get_or_create_histogram(
    "keep_frontend_web_vital_cls",
    "Cumulative Layout Shift",
    ["path"],
    buckets=(0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0)
)

# FCP: 1.8s (good), 3.0s (needs improvement)
FCP_HISTOGRAM = get_or_create_histogram(
    "keep_frontend_web_vital_fcp",
    "First Contentful Paint (seconds)",
    ["path"],
    buckets=(0.5, 1.0, 1.5, 1.8, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0)
)

# LCP: 2.5s (good), 4.0s (needs improvement)
LCP_HISTOGRAM = get_or_create_histogram(
    "keep_frontend_web_vital_lcp",
    "Largest Contentful Paint (seconds)",
    ["path"],
    buckets=(0.5, 1.0, 1.5, 2.5, 3.0, 4.0, 5.0, 10.0)
)

# TTFB: 0.8s (good), 1.8s (needs improvement)
TTFB_HISTOGRAM = get_or_create_histogram(
    "keep_frontend_web_vital_ttfb",
    "Time to First Byte (seconds)",
    ["path"],
    buckets=(0.1, 0.2, 0.5, 0.8, 1.0, 1.8, 3.0, 5.0)
)

# FID: 100ms (good), 300ms (needs improvement)
FID_HISTOGRAM = get_or_create_histogram(
    "keep_frontend_web_vital_fid",
    "First Input Delay (seconds)",
    ["path"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 1.0)
)

# INP: 200ms (good), 500ms (needs improvement)
INP_HISTOGRAM = get_or_create_histogram(
    "keep_frontend_web_vital_inp",
    "Interaction to Next Paint (seconds)",
    ["path"],
    buckets=(0.1, 0.2, 0.5, 1.0, 2.0, 5.0)
)

METRICS_MAP = {
    "CLS": CLS_HISTOGRAM,
    "FCP": FCP_HISTOGRAM,
    "LCP": LCP_HISTOGRAM,
    "TTFB": TTFB_HISTOGRAM,
    "FID": FID_HISTOGRAM,
    "INP": INP_HISTOGRAM,
}

class WebVitalMetric(BaseModel):
    id: str
    name: str # CLS, FCP, LCP, etc.
    delta: float
    value: float
    rating: str # good, needs-improvement, poor
    navigationType: str
    path: str

@router.post("")
async def collect_rum_metrics(metric: WebVitalMetric):
    histogram = METRICS_MAP.get(metric.name)
    if histogram:
        # Sanitize path to avoid high cardinality (optional, but good practice)
        # For now, we take the path as provided by the frontend (which should be the route pattern ideally)
        # In Next.js, useReportWebVitals might give the actual path.
        # We will trust the frontend to send a generic path or handle sanitation there.
        try:
            # Web Vitals delta is usually what we want for accumulation, but for Histogram observe() we use value.
            # However, for CLS, delta is important for accumulation, but standard histograms observe the 'value' at report time.
            # useReportWebVitals reports the current value.
            
            # Most WebVitals libs report milliseconds, but our buckets might be in seconds or mixed.
            # Next.js web-vitals reports:
            # CLS: unitless
            # FID, LCP, FCP, TTFB, INP: milliseconds
            
            value = metric.value
            
            # Convert ms to seconds for time-based metrics to align with standard Prometheus practices
            if metric.name in ["FCP", "LCP", "TTFB", "FID", "INP"]:
                value = value / 1000.0

            histogram.labels(path=metric.path).observe(value)
        except Exception:
            # Fail silently to not impact the user
            pass
    return {"status": "ok"}
