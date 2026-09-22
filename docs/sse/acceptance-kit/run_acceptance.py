#!/usr/bin/env python3
"""Acceptance checks for the real-time SSE work, against any Keep environment.

Every check prints the measured value next to the threshold it is judged by, so
a run is readable on its own. Exit code 0 = all selected checks passed.

    ./run_acceptance.py                      # non-disruptive checks only
    ./run_acceptance.py --disruptive         # + pod delete, rolling restart, Redis restart
    ./run_acceptance.py --only fanout,token  # a subset
    ./run_acceptance.py --json report.json   # machine-readable results as well

Configuration comes from the environment (see config.env.example):
    NAMESPACE, GATEWAY_ROUTE, GATEWAY_SELECTOR, TENANT_ID,
    auth: KEEP_BEARER | (KC_URL, KC_REALM, KC_CLIENT_ID, KC_CLIENT_SECRET, KC_USER, KC_PASSWORD) | KEEP_API_KEY
    SSE_NOTIFY_TOKEN (optional, enables the token check),
    REDIS_DEPLOY (optional, e.g. deploy/valkey - enables the Redis-restart check),
    FANOUT_CHANNEL (optional, defaults to "<REDIS_KEY_PREFIX>keep:sse" read from the gateway)

Nothing here writes to a repository. The disruptive checks restart pods in the
target namespace, so run them on dev/integration, never production.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

NS = os.environ.get("NAMESPACE", "")
ROUTE = os.environ.get("GATEWAY_ROUTE", "").rstrip("/")
SELECTOR = os.environ.get("GATEWAY_SELECTOR", "app=keep-api-gateway")
TENANT = os.environ.get("TENANT_ID", "keep")
NOTIFY_TOKEN = os.environ.get("SSE_NOTIFY_TOKEN") or None
REDIS_DEPLOY = os.environ.get("REDIS_DEPLOY") or None
API_KEY = os.environ.get("KEEP_API_KEY") or None
RESULTS: list[dict] = []


# ---------------------------------------------------------------- plumbing ---
def oc(*args: str, check: bool = False) -> str:
    cmd = ["oc"] + (["-n", NS] if NS else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {p.stderr.strip()[:200]}")
    return p.stdout.strip()


def gateway_pods() -> list[str]:
    return oc("get", "pods", "-l", SELECTOR, "--field-selector=status.phase=Running",
              "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}").split()


def pod_env(pod: str, name: str) -> str:
    return oc("exec", pod, "--", "sh", "-c", f"printf %s \"${name}\"")


def pod_metrics(pod: str) -> dict[str, float]:
    out = oc("exec", pod, "--", "python3", "-c",
             "import urllib.request;print(urllib.request.urlopen("
             "'http://localhost:8080/metrics',timeout=15).read().decode())")
    m = {}
    for line in out.splitlines():
        if line.startswith("keep_sse") and not line.startswith("#"):
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                try:
                    m[parts[0]] = float(parts[1])
                except ValueError:
                    pass
    return m


def bearer() -> str | None:
    if os.environ.get("KEEP_BEARER"):
        return os.environ["KEEP_BEARER"]
    kc = os.environ.get("KC_URL")
    if not kc:
        return None
    body = urllib.parse.urlencode({
        "client_id": os.environ.get("KC_CLIENT_ID", "keep"),
        "client_secret": os.environ.get("KC_CLIENT_SECRET", ""),
        "username": os.environ["KC_USER"], "password": os.environ["KC_PASSWORD"],
        "grant_type": "password", "scope": "openid"}).encode()
    realm = os.environ.get("KC_REALM", "keep")
    url = f"{kc.rstrip('/')}/realms/{realm}/protocol/openid-connect/token"
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30) as r:
        return json.load(r)["access_token"]


def auth_header() -> dict[str, str]:
    """How this environment authenticates a stream / an ingest."""
    tok = bearer()
    if tok:
        return {"Authorization": f"Bearer keepActiveTenant={TENANT}&{tok}"}
    if API_KEY:
        return {"x-api-key": API_KEY}
    return {}


def http(method: str, url: str, headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Stream:
    """One SSE subscription, read on a background thread."""

    def __init__(self, base: str, headers: dict, label: str = "s"):
        self.url = base.rstrip("/") + "/sse/subscribe"
        self.headers = {"Accept": "text/event-stream", **headers}
        self.label = label
        self.events: list[tuple[float, str, str]] = []
        self.connected = threading.Event()
        self.connected_payload = None
        self.eof_at = None
        self.error = None
        self._resp = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            req = urllib.request.Request(self.url, method="POST", headers=self.headers)
            self._resp = urllib.request.urlopen(req, timeout=900)
            event = None
            while True:
                raw = self._resp.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    payload = line[5:].strip()
                    if event == "connected":
                        self.connected_payload = json.loads(payload)
                        self.connected.set()
                    self.events.append((time.time(), event or "", payload))
                elif line.startswith(":"):
                    self.events.append((time.time(), "comment", line))
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
        finally:
            self.eof_at = time.time()

    def wait_ready(self, timeout=30):
        if not self.connected.wait(timeout):
            raise RuntimeError(f"{self.label}: no 'connected' event ({self.error})")
        return self

    def seen(self, marker: str):
        for ts, _ev, data in self.events:
            if marker in data:
                return ts
        return None

    def close(self):
        try:
            if self._resp:
                self._resp.close()
        except Exception:  # noqa: BLE001
            pass


def notify(base: str, event="poll-alerts", data=None, token=NOTIFY_TOKEN):
    headers = {"X-Keep-Notify-Token": token} if token else {}
    status, _ = http("POST", base.rstrip("/") + "/sse/notify", headers,
                     {"tenant_id": TENANT, "event": event, "data": data or {}}, timeout=15)
    return status


def forwards(pods: list[str], first_port=9601):
    procs, urls = [], {}
    for i, pod in enumerate(pods):
        port = first_port + i
        procs.append(subprocess.Popen(
            ["oc"] + (["-n", NS] if NS else []) + ["port-forward", f"pod/{pod}", f"{port}:8080"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        urls[pod] = f"http://localhost:{port}"
    time.sleep(6)
    return procs, urls


def record(name: str, passed: bool, measured, expected: str, note: str = ""):
    RESULTS.append({"check": name, "pass": bool(passed), "measured": measured,
                    "expected": expected, "note": note})
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n"
          f"       measured: {measured}\n       expected: {expected}"
          + (f"\n       note: {note}" if note else ""), flush=True)


# ----------------------------------------------------------------- checks ---
def check_preflight():
    pods = gateway_pods()
    if not pods:
        record("preflight/pods", False, "no gateway pods found",
               f"pods matching {SELECTOR} in {NS}")
        return pods
    fan, prefix, connected = [], [], []
    for pod in pods:
        fan.append(pod_env(pod, "SSE_FANOUT") or "none")
        prefix.append(pod_env(pod, "REDIS_KEY_PREFIX") or "")
        connected.append(pod_metrics(pod).get("keep_sse_fanout_connected", 0.0))
    record("preflight/replicas", len(pods) > 1, f"{len(pods)} gateway pods",
           ">1 (with 1 pod the cross-pod defect cannot appear at all)")
    record("preflight/fanout-enabled", all(f == "redis" for f in fan),
           f"SSE_FANOUT={sorted(set(fan))}", "redis on every pod")
    record("preflight/fanout-connected", all(c == 1.0 for c in connected),
           f"keep_sse_fanout_connected={connected}", "1.0 on every pod")
    record("preflight/key-prefix", all(p.strip() for p in prefix),
           f"REDIS_KEY_PREFIX={sorted(set(prefix))}",
           "non-empty and unique per deployment (a shared Redis otherwise mixes deployments)")
    return pods


def check_config(pods):
    """Print the configuration the cluster is actually running, and judge the
    parts that have a right answer. Use this to confirm what Helm rendered."""
    gw = SELECTOR.split("=")[-1]
    for dep in (gw, os.environ.get("EH_DEPLOY", "keep-event-handler"),
                os.environ.get("WF_DEPLOY", "keep-workflows")):
        env = oc("get", "deploy", dep,
                 "-o", "jsonpath={range .spec.template.spec.containers[0].env[*]}"
                       "{.name}={.value}{\"\\n\"}{end}")
        keep = [l for l in env.splitlines()
                if l.split("=")[0] in {"SSE_FANOUT", "SSE_FANOUT_CHANNEL", "REDIS_HOST",
                                       "REDIS_PORT", "REDIS_DB", "REDIS_SSL", "REDIS_KEY_PREFIX",
                                       "REDIS_SENTINEL_ENABLED", "REDIS_SENTINEL_HOSTS",
                                       "SSE_NOTIFY_TOKEN", "SSE_KEEPALIVE_INTERVAL_SECONDS",
                                       "KEEP_WORKERS", "KEEP_API_URL"}]
        keep = [("SSE_NOTIFY_TOKEN=<set>" if l.startswith("SSE_NOTIFY_TOKEN=") else l) for l in keep]
        print(f"       {dep}: {' '.join(keep) or '(none of the SSE settings are set)'}")
    prefix = pod_env(pods[0], "REDIS_KEY_PREFIX")
    base = pod_env(pods[0], "SSE_FANOUT_CHANNEL") or "keep:sse"
    workers = pod_env(pods[0], "KEEP_WORKERS") or "1"
    processes = len(pods) * int(workers or 1)
    record("config/channel", bool(prefix.strip()),
           f"channel '{prefix}{base}', {len(pods)} pods x KEEP_WORKERS={workers} "
           f"= {processes} broker processes -> expect {processes} subscribers on that channel",
           "a non-empty REDIS_KEY_PREFIX; REDIS_DB does NOT isolate pub/sub, only the prefix does")


def check_fanout(pods):
    """Notify ONE pod; every pod's subscriber must receive it."""
    procs, urls = forwards(pods)
    try:
        streams = {p: Stream(u, auth_header(), p).wait_ready() for p, u in urls.items()}
        time.sleep(2)
        target = pods[0]
        n, delivered = 10, {p: 0 for p in pods}
        statuses = set()
        for _ in range(n):
            marker = f"acc-{uuid.uuid4().hex[:10]}"
            statuses.add(notify(urls[target], data={"alerts": [{"fingerprint": marker}]}))
            deadline = time.time() + 5
            while time.time() < deadline:
                if all(streams[p].seen(marker) for p in pods):
                    break
                time.sleep(0.02)
            for p in pods:
                if streams[p].seen(marker):
                    delivered[p] += 1
            time.sleep(0.2)
        ok = all(v == n for v in delivered.values()) and statuses <= {204}
        note = ""
        if 401 in statuses:
            note = ("the notify route rejected these calls (401), so this says nothing about "
                    "fan-out: set SSE_NOTIFY_TOKEN in the config to the value the gateway runs with")
        elif not ok and delivered.get(target) == n:
            note = ("only the notified pod received them - this is the per-pod isolation defect: "
                    "check SSE_FANOUT/Redis connectivity")
        record("fanout/cross-pod", ok,
               f"{ {p[-5:]: f'{v}/{n}' for p, v in delivered.items()} }, notify HTTP {statuses}",
               f"{n}/{n} on every pod, HTTP 204 "
               "(before the fix: only the notified pod, and still 204 - loss is silent)", note)
        for s in streams.values():
            s.close()
    finally:
        for p in procs:
            p.terminate()


def check_connected(pods):
    procs, urls = forwards(pods[:1], 9701)
    try:
        s = Stream(urls[pods[0]], auth_header()).wait_ready()
        payload = s.connected_payload or {}
        record("shutdown/keepalive-announced", "keepalive_seconds" in payload,
               f"connected payload {payload}",
               "contains keepalive_seconds (lets the client size its watchdog)")
        s.close()
    finally:
        for p in procs:
            p.terminate()


def check_token():
    if not NOTIFY_TOKEN:
        record("security/notify-token", False, "SSE_NOTIFY_TOKEN not set in this environment",
               "set it on the event handler and workflows first, then on the gateway", "skipped")
        return
    no_hdr = notify(ROUTE, token=None)
    wrong = notify(ROUTE, token="wrong-token")
    right = notify(ROUTE)
    record("security/notify-token", no_hdr == 401 and wrong == 401 and right == 204,
           f"no header {no_hdr}, wrong {wrong}, correct {right}",
           "401 / 401 / 204 (the notify route reaches every browser, so it must not be open)")


def check_end_to_end(pods):
    """Real alerts through the public route; every pod's viewer must see them."""
    procs, urls = forwards(pods, 9801)
    try:
        streams = {p: Stream(u, auth_header(), p).wait_ready() for p, u in urls.items()}
        time.sleep(2)
        n = 5
        posted = []
        for i in range(n):
            fp = f"acc-e2e-{uuid.uuid4().hex[:8]}"
            status, _ = http("POST", f"{ROUTE}/alerts/event", auth_header(), {
                "name": fp, "fingerprint": fp, "status": "firing", "severity": "critical",
                "source": ["sse-acceptance"],
                "lastReceived": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())})
            posted.append((fp, status))
            time.sleep(2)
        time.sleep(20)
        per_pod = {p: sum(1 for fp, _ in posted if streams[p].seen(fp)) for p in pods}
        ok = all(v == n for v in per_pod.values()) and all(s in (200, 202) for _, s in posted)
        record("delivery/end-to-end", ok,
               f"{ {p[-5:]: f'{v}/{n}' for p, v in per_pod.items()} }, ingest HTTP "
               f"{sorted({s for _, s in posted})}",
               f"{n}/{n} on every pod - this is what a viewer pinned to that pod would see")
        for s in streams.values():
            s.close()
    finally:
        for p in procs:
            p.terminate()


def check_poll_presets(pods):
    window = "20m"
    counts = {p: int(oc("logs", p, f"--since={window}").count("poll-presets")) for p in pods[:2]}
    record("d2/poll-presets-removed", all(v == 0 for v in counts.values()),
           f"poll-presets log lines in the last {window}: {counts}",
           "0 (the event handler no longer computes or sends preset notifications)")


def check_fanout_errors(pods):
    bad = {}
    for p in pods:
        m = pod_metrics(p)
        errs = {k: v for k, v in m.items() if "fanout_errors" in k and v > 0}
        if errs:
            bad[p[-5:]] = errs
    record("fanout/errors", not bad, f"non-zero fan-out error counters: {bad or 'none'}",
           "none (decode/schema counters only rise if something publishes malformed payloads)")


def check_pod_delete(pods):
    """A rollout must not strand viewers on a dying pod."""
    target = pods[0]
    procs, urls = forwards([target], 9901)
    try:
        s = Stream(urls[target], auth_header()).wait_ready()
        time.sleep(2)
        t0 = time.time()
        oc("delete", "pod", target, "--wait=false")
        while time.time() < t0 + 60 and s.eof_at is None:
            time.sleep(0.05)
        eof = round(s.eof_at - t0, 2) if s.eof_at else None
        record("shutdown/stream-closes-on-sigterm", eof is not None and eof < 5,
               f"stream ended {eof}s after the pod was deleted",
               "<5s (before the fix: ~30s, the gunicorn force-kill, with keepalives still flowing)")
        s.close()
    finally:
        for p in procs:
            p.terminate()
    oc("rollout", "status", f"deploy/{SELECTOR.split('=')[-1]}", "--timeout=300s")


def check_rolling(pods):
    """Alerts ingested during a full rolling restart must still reach viewers."""
    seen, lock, stop = {}, threading.Lock(), threading.Event()
    hdr = auth_header()

    def subscriber(i):
        while not stop.is_set():
            try:
                s = Stream(ROUTE, hdr, f"sub{i}")
                s.wait_ready(timeout=30)
                while not stop.is_set() and s.eof_at is None:
                    time.sleep(0.2)
                with lock:
                    for ts, ev, data in s.events:
                        if ev == "poll-alerts":
                            for al in (json.loads(data).get("alerts") or []):
                                seen.setdefault(al.get("fingerprint"), set()).add(i)
                s.close()
            except Exception:  # noqa: BLE001
                pass
            if not stop.is_set():
                time.sleep(1)

    subs = [threading.Thread(target=subscriber, args=(i,), daemon=True) for i in range(3)]
    for t in subs:
        t.start()
    time.sleep(10)
    posted = []

    def ingest():
        for _ in range(30):
            fp = f"acc-roll-{uuid.uuid4().hex[:8]}"
            http("POST", f"{ROUTE}/alerts/event", hdr, {
                "name": fp, "fingerprint": fp, "status": "firing", "severity": "critical",
                "source": ["sse-acceptance"],
                "lastReceived": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())})
            posted.append(fp)
            time.sleep(2)

    ing = threading.Thread(target=ingest, daemon=True)
    ing.start()
    time.sleep(10)
    deploy = SELECTOR.split("=")[-1]
    oc("rollout", "restart", f"deploy/{deploy}")
    oc("rollout", "status", f"deploy/{deploy}", "--timeout=600s")
    ing.join()
    time.sleep(20)
    stop.set()
    time.sleep(2)
    pairs = sum(len(seen.get(fp, ())) for fp in posted)
    ratio = pairs / (len(posted) * 3) if posted else 0
    record("delivery/through-rolling-restart", ratio >= 0.95,
           f"{pairs}/{len(posted) * 3} viewer-alert pairs delivered ({ratio:.0%})",
           ">=95% (before the fix this was ~15%); raw subscribers have no catch-up, "
           "so the browser does better than this number")


def check_redis_restart(pods):
    if not REDIS_DEPLOY:
        record("fanout/redis-restart", False, "REDIS_DEPLOY not set",
               "set REDIS_DEPLOY=deploy/<redis> to exercise it", "skipped")
        return
    procs, urls = forwards(pods[:2], 9401)
    try:
        sub, target = pods[0], pods[1]
        s = Stream(urls[sub], auth_header()).wait_ready()
        oc("rollout", "restart", REDIS_DEPLOY)
        oc("rollout", "status", REDIS_DEPLOY, "--timeout=300s")
        survived = s.eof_at is None
        delivered = 0
        for attempt in range(6):
            time.sleep(5)
            marker = f"acc-redis-{uuid.uuid4().hex[:8]}"
            notify(urls[target], data={"alerts": [{"fingerprint": marker}]})
            deadline = time.time() + 5
            while time.time() < deadline and not s.seen(marker):
                time.sleep(0.05)
            if s.seen(marker):
                delivered += 1
                if delivered >= 3:
                    break
        record("fanout/redis-restart", survived and delivered >= 3,
               f"stream survived={survived}, cross-pod deliveries after restart={delivered}",
               "stream survives and cross-pod delivery resumes within ~30s")
        s.close()
    finally:
        for p in procs:
            p.terminate()


# ------------------------------------------------------------------- main ---
CHECKS = {
    "config": check_config,
    "fanout": check_fanout,
    "connected": check_connected,
    "token": lambda pods: check_token(),
    "e2e": check_end_to_end,
    "presets": check_poll_presets,
    "errors": check_fanout_errors,
}
DISRUPTIVE = {
    "poddelete": check_pod_delete,
    "rolling": check_rolling,
    "redis": check_redis_restart,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disruptive", action="store_true",
                    help="also restart pods / Redis (dev and integration only)")
    ap.add_argument("--only", default="", help="comma-separated subset of check names")
    ap.add_argument("--json", default="", help="write results to this file as JSON")
    a = ap.parse_args()
    if not ROUTE or not NS:
        sys.exit("NAMESPACE and GATEWAY_ROUTE must be set (see config.env.example)")

    print(f"namespace={NS} route={ROUTE} tenant={TENANT} "
          f"auth={'bearer' if bearer() else ('api-key' if API_KEY else 'none')}\n")
    pods = check_preflight()
    if not pods:
        sys.exit(2)
    selected = dict(CHECKS)
    if a.disruptive:
        selected.update(DISRUPTIVE)
    if a.only:
        wanted = {n.strip() for n in a.only.split(",")}
        selected = {k: v for k, v in {**CHECKS, **DISRUPTIVE}.items() if k in wanted}
    for name, fn in selected.items():
        try:
            fn(pods)
            pods = gateway_pods() or pods
        except Exception as exc:  # noqa: BLE001
            record(name, False, f"check raised {type(exc).__name__}: {exc}", "check to complete")

    failed = [r for r in RESULTS if not r["pass"] and r.get("note") != "skipped"]
    skipped = [r for r in RESULTS if r.get("note") == "skipped"]
    print(f"\n{len(RESULTS) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
          f"{len(skipped)} skipped")
    for r in failed:
        print(f"  FAILED: {r['check']} -> {r['measured']}")
    if a.json:
        json.dump(RESULTS, open(a.json, "w"), indent=2)
        print(f"results written to {a.json}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
