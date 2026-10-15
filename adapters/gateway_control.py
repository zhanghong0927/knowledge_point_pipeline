"""Control a loopback inference gateway between sequential model phases."""

import ipaddress
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler


def _request(api_url, method, path, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(api_url + path, data=data, method=method,
                      headers={"Content-Type": "application/json"} if data is not None else {})
    # A loopback control call must not be routed through a configured proxy.
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("gateway control returned a non-object response")
    return result


def transition_phase(api_url, limit, phase, timeout=1800, poll_interval=0.1):
    """Drain this gateway's active clients, then set a phase cap and resume.

    Queued clients may remain while paused. This does not assert that other
    clients using the upstream directly, or another gateway, are idle.
    """
    parsed = urlsplit(api_url)
    try:
        host = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise ValueError("api_url must use a literal loopback address") from exc
    if (parsed.scheme != "http" or not host.is_loopback or parsed.username or
            parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("api_url must be a loopback HTTP origin")
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 128:
        raise ValueError("phase must be a nonempty string of at most 128 characters")
    if type(timeout) not in (int, float) or timeout <= 0 or type(poll_interval) not in (int, float) or poll_interval <= 0:
        raise ValueError("timeout and poll_interval must be positive")
    deadline = time.monotonic() + timeout
    _request(api_url, "POST", "/_scheduler/control", {"paused": True},
             timeout=max(0.001, min(10, deadline - time.monotonic())))
    while True:
        state = _request(api_url, "GET", "/_scheduler/stats",
                         timeout=max(0.001, min(10, deadline - time.monotonic())))
        if state["total"]["active"] == 0:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"gateway phase drain timed out: active={state['total']['active']}, "
                f"queued={state['total']['queued']}; gateway remains paused")
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
    if time.monotonic() >= deadline:
        raise TimeoutError("gateway phase drain timed out; gateway remains paused")
    # Validate and apply the new cap while admission is still paused.
    _request(api_url, "POST", "/_scheduler/control",
             {"limit": limit, "phase": phase, "paused": True},
             timeout=max(0.001, min(10, deadline - time.monotonic())))
    try:
        state = _request(api_url, "POST", "/_scheduler/control", {"paused": False},
                         timeout=max(0.001, min(10, deadline - time.monotonic())))
        if state.get("paused") is not False or state.get("limit") != limit or state.get("phase") != phase:
            raise RuntimeError("gateway did not confirm the requested phase")
    except Exception:
        try:
            _request(api_url, "POST", "/_scheduler/control", {"paused": True})
        except (HTTPError, URLError, TimeoutError, OSError):
            pass
        raise
    return state
