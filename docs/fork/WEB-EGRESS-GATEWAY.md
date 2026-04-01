# Web Egress Gateway — Design & Feasibility

_Branch: `feature/browser-tool`_

## Problem Statement

The Linux GPU server running hermes-agent-core enforces a **Tailscale-only** networking policy: all inbound and outbound traffic is required to traverse the Tailscale interface (`tailscale0`). Direct internet egress from the Docker bridge is blocked at the iptables level.

Per-user runtime containers run with **gVisor** (`runsc`) as the Docker isolation runtime. These containers need to reach the web to drive:

| Tool | Mechanism | Needs |
|------|-----------|-------|
| `web_search_tool` | Firecrawl / Tavily / Parallel cloud APIs | HTTPS to external APIs |
| `web_extract_tool` | Same backends | HTTPS to external APIs + target URLs |
| `web_crawl_tool` | Firecrawl API | HTTPS to Firecrawl + target URLs |
| `browser_navigate` | Local Chromium via `agent-browser` CLI | HTTPS to any URL |

None of these work today in the Tailscale-only network because container egress to the public internet is blocked.

---

## Network Topology

```
┌────────────────────────────────────────────────────────────┐
│  Linux GPU Server                                          │
│                                                            │
│  ┌──────────────────────────────────────────┐             │
│  │  Docker runtime (gVisor / runsc)          │             │
│  │                                           │             │
│  │  ┌─────────────┐   ┌─────────────┐       │             │
│  │  │ User A      │   │ User B      │       │             │
│  │  │ runtime     │   │ runtime     │       │             │
│  │  │ container   │   │ container   │       │             │
│  │  └──────┬──────┘   └──────┬──────┘       │             │
│  │         │                 │               │             │
│  └─────────┼─────────────────┼───────────────┘             │
│            │ docker0 bridge  │                             │
│            └────────┬────────┘                             │
│                     │ 172.x.0.1 (host gateway)            │
│                     │                                      │
│   ┌─────────────────▼─────────────────────┐               │
│   │  Host services                         │               │
│   │  • web-egress-gateway  :8765           │               │
│   │  • Tailscale daemon                    │               │
│   └─────────────────┬─────────────────────┘               │
│                     │                                      │
└─────────────────────┼──────────────────────────────────────┘
                      │ tailscale0
                      ▼
              Tailscale network
                      │
              [exit node]  ← optional: routes to public internet
                      │
                   Internet
```

### Key facts about this topology

1. **gVisor netstack**: gVisor implements TCP/IP in userspace. It does NOT block outbound connections — it passes them through the veth/bridge to the host. Network isolation at the host iptables level is what blocks them.

2. **`host.docker.internal` is already wired**: `docker_run_command()` in `product_runtime_container.py:94` already adds `--add-host host.docker.internal:host-gateway`, so every container can reach the Docker bridge gateway (= host) by hostname.

3. **Only the host process can use Tailscale egress**: The `tailscale0` interface is on the host network namespace. Container processes cannot route through it directly.

4. **Tailscale exit nodes**: A Tailscale exit node is a peer on the tailnet that forwards internet-bound traffic. The host can be configured to use one, making all host-originated HTTP requests reach the internet via the tailnet.

---

## Feasibility Assessment

**Yes, a safe web egress gateway is feasible.** The architecture is:

1. A **gateway service** runs on the host (outside gVisor), bound to the Docker bridge interface.
2. Runtime containers send all web requests to the gateway via `http://host.docker.internal:8765`.
3. The gateway validates, logs, and forwards requests outbound via the Tailscale interface.
4. iptables ensures containers cannot bypass the gateway.

This is a well-established pattern (e.g., Squid proxy in corporate environments, sidecar egress proxies in Kubernetes).

---

## Implementation Options

### Option A: Transparent HTTP/SOCKS5 Proxy (simplest)

Run a standard proxy (tinyproxy, Squid, or Dante SOCKS5) on the host. Configure containers with `HTTP_PROXY` / `HTTPS_PROXY` env vars.

**Pros**: No code changes to web tools. `httpx`, `requests`, and Chromium all respect these env vars.

**Cons**:
- Less control — any URL is proxied unless manually blocked
- Firecrawl Python SDK uses `requests`, which respects proxy env vars, but SDK internals are harder to audit
- Chromium does NOT respect `HTTP_PROXY` env var — needs `--proxy-server` CLI flag
- HTTPS traffic is tunneled (HTTP CONNECT), so the proxy can't inspect content — URL-level allowlisting only

**Verdict**: Viable as a stopgap but lacks the control and observability needed for a secure agent deployment.

---

### Option B: Self-Hosted Firecrawl on Tailnet Node (clean separation)

Run a [self-hosted Firecrawl](https://docs.firecrawl.dev/contributing/self-host) instance on a separate Tailscale-connected node. The web tools already support this via `FIRECRAWL_API_URL`.

**Pros**:
- Zero code changes — point `FIRECRAWL_API_URL` at the Firecrawl node's Tailscale IP
- Firecrawl handles search + extract + crawl
- Strong isolation — agent never makes direct HTTP requests to the internet

**Cons**:
- Requires a second node/VM in the tailnet
- `browser_tool` (local Chromium) still needs a proxy
- Firecrawl self-hosting is complex (Redis, Playwright workers, etc.)
- You lose the ability to add custom URL policies or audit logs at the gateway layer

**Verdict**: Good for search/extract, but doesn't solve the browser tool problem.

---

### Option C: Custom Web Egress Gateway Service (recommended)

A purpose-built Python/FastAPI service running on the host that:
1. Implements the **Firecrawl v1 API surface** (search, scrape, crawl) — so `FIRECRAWL_API_URL` works unchanged
2. Also runs a **SOCKS5 proxy** (via `python-socks` or `dante`) for Chromium
3. Binds to the Docker bridge interface (`172.x.0.1`) only — not reachable from outside the host
4. Makes all outbound HTTP requests **bound to `tailscale0`** using `httpx` with interface binding
5. Enforces a **URL allowlist/blocklist**, rate limiting, and per-user attribution

**Architecture within the codebase**:

```
tools/web_gateway/
├── __init__.py
├── gateway_server.py      # FastAPI app — Firecrawl-compatible API
├── gateway_client.py      # httpx client bound to tailscale0
├── socks_proxy.py         # Async SOCKS5 mini-proxy for Chromium
├── url_policy.py          # URL allowlist/blocklist enforcement
└── service.py             # systemd service wrapper / start/stop

hermes_cli/
├── product_web_gateway.py # Gateway lifecycle (start with product stack)
└── product_runtime_staging.py  # +WEB_GATEWAY_URL, +BROWSER_PROXY_URL env vars
```

---

## Detailed Design: Custom Gateway (Option C)

### 1. Gateway Server (`tools/web_gateway/gateway_server.py`)

FastAPI app implementing the Firecrawl v1 API:

```
POST /v1/scrape          → fetch URL, return markdown
POST /v1/search          → search via SearXNG or Brave Search API (on tailnet)
POST /v1/crawl           → crawl site
GET  /v1/crawl/{id}      → crawl status
GET  /health             → liveness
```

The server:
- Listens on `host.docker.internal:8765` (Docker bridge only)
- Authenticates requests with a shared secret from `HERMES_WEB_GATEWAY_TOKEN`
- Logs all requests with user attribution from the `X-Hermes-User` header
- Enforces URL policy before fetching

### 2. Outbound HTTP Client (`tools/web_gateway/gateway_client.py`)

Uses `httpx.AsyncClient` with the Tailscale IP bound as the source address:

```python
import httpx
import socket

def _make_tailscale_client() -> httpx.AsyncClient:
    """httpx client whose connections originate from the tailscale0 interface."""
    ts_ip = _get_tailscale_ip()          # reads from tailscale0 via netifaces
    transport = httpx.AsyncHTTPTransport(
        local_address=ts_ip,             # bind source IP to tailscale0
        retries=2,
    )
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": "hermes-web-gateway/1.0"},
    )
```

By binding the source IP to the Tailscale address, the OS routes all connections through `tailscale0`. Combined with iptables rules that block docker0→internet, this ensures:
- Requests from the gateway use Tailscale
- Containers cannot bypass the gateway

### 3. SOCKS5 Mini-Proxy for Chromium (`tools/web_gateway/socks_proxy.py`)

A minimal async SOCKS5 server (RFC 1928) that:
- Listens on `host.docker.internal:1080`
- Forwards connections via the same Tailscale-bound `httpx` transport
- Applies the same URL policy before establishing TCP connections

This handles `browser_tool` since Chromium supports `--proxy-server=socks5://host.docker.internal:1080`.

### 4. URL Policy (`tools/web_gateway/url_policy.py`)

```python
# Default policy: allow most web traffic, block internal/cloud-metadata
BLOCKED_PATTERNS = [
    r"^https?://169\.254\.",          # AWS/GCP metadata service
    r"^https?://10\.",                # RFC1918 — internal
    r"^https?://192\.168\.",          # RFC1918 — internal
    r"^https?://172\.(1[6-9]|2\d|3[01])\.",  # RFC1918 — internal
    r"^https?://localhost",           # loopback
    r"^https?://.*\.internal",        # internal DNS
]
```

### 5. Runtime Environment Changes (`hermes_cli/product_runtime_staging.py`)

```python
def runtime_environment(...) -> dict[str, str]:
    env = {
        # ...existing vars...
    }
    # Web egress gateway config — injected when gateway is enabled
    if gateway_enabled(product_config):
        env["FIRECRAWL_API_URL"] = "http://host.docker.internal:8765"
        env["FIRECRAWL_API_KEY"] = gateway_token(product_config)
        env["BROWSER_PROXY_URL"] = "socks5://host.docker.internal:1080"
        # httpx/requests transparent proxy (belt-and-suspenders)
        env["HTTP_PROXY"] = "http://host.docker.internal:8765"
        env["HTTPS_PROXY"] = "http://host.docker.internal:8765"
    return env
```

### 6. Browser Tool Proxy Injection (`tools/browser_tool.py`)

When `BROWSER_PROXY_URL` is set, inject the proxy flag into the `agent-browser` command:

```python
# In _run_browser_command():
proxy_url = os.environ.get("BROWSER_PROXY_URL", "").strip()
if proxy_url and not session_info.get("cdp_url"):
    # Local Chromium mode — inject proxy-server flag
    # agent-browser passes extra args after '--' to Chromium
    cmd_parts += ["--", f"--proxy-server={proxy_url}"]
```

---

## iptables Enforcement

On the Linux host, add rules to prevent containers from bypassing the gateway:

```bash
# Allow container → gateway (HTTP API + SOCKS5)
iptables -I DOCKER-USER -s 172.17.0.0/16 -d <docker-bridge-ip> -p tcp --dport 8765 -j ACCEPT
iptables -I DOCKER-USER -s 172.17.0.0/16 -d <docker-bridge-ip> -p tcp --dport 1080 -j ACCEPT

# Block container → direct internet (non-tailscale)
# (Tailscale traffic to tailscale0 is not affected since it's a different interface)
iptables -I DOCKER-USER -s 172.17.0.0/16 -o eth0 -j DROP
iptables -I DOCKER-USER -s 172.17.0.0/16 -o wlan0 -j DROP

# Allow gateway process → tailscale0 (enforced by binding source IP above,
# but belt-and-suspenders):
iptables -I OUTPUT -m owner --uid-owner hermes-gateway -o tailscale0 -j ACCEPT
iptables -I OUTPUT -m owner --uid-owner hermes-gateway ! -o tailscale0 -j DROP
```

These rules are idempotent and should be applied via the product install script or a dedicated `ip-rules` systemd service that runs before Docker.

---

## Tailscale Exit Node

For the gateway to reach the public internet, the host must route via a Tailscale exit node:

```bash
# On the GPU server: use a tailnet exit node for outbound internet
tailscale up --exit-node=<exit-node-hostname> --exit-node-allow-lan-access=false
```

Or, if a dedicated search/browsing peer exists on the tailnet (preferred — keeps the GPU server isolated):

```bash
# On the GPU server: do NOT use exit node
# Instead, in gateway_client.py, proxy through a tailnet peer that runs
# tinyproxy/squid and DOES have internet access
GATEWAY_UPSTREAM_PROXY=http://100.x.y.z:3128
```

This keeps the GPU server off the internet entirely — even the gateway process itself doesn't have direct internet access, it proxies through a designated tailnet peer.

---

## Security Properties

| Property | Mechanism |
|----------|-----------|
| Container cannot reach internet directly | iptables `DOCKER-USER` chain drops container→eth0 |
| Container cannot reach host metadata | URL policy blocks RFC1918 + `169.254.x.x` |
| Gateway cannot be reached from outside the host | Gateway binds to Docker bridge IP only |
| All web requests are auditable | Gateway logs all requests with user + URL |
| Gateway cannot reach LAN | iptables limits gateway process to tailscale0 egress |
| Token required to use gateway | `X-API-Key` header checked against `HERMES_WEB_GATEWAY_TOKEN` |
| gVisor provides process isolation | Even if a container is compromised, it cannot escape gVisor sandbox |

---

## What Needs Building

### Phase 1: Core gateway (unblocks web_tools)
- [ ] `tools/web_gateway/gateway_server.py` — FastAPI, Firecrawl-compatible API
- [ ] `tools/web_gateway/gateway_client.py` — httpx client bound to tailscale0
- [ ] `tools/web_gateway/url_policy.py` — URL blocklist
- [ ] `hermes_cli/product_web_gateway.py` — systemd unit + start/stop helpers
- [ ] `hermes_cli/product_runtime_staging.py` — inject `FIRECRAWL_API_URL` + `HTTP_PROXY`
- [ ] Product install: iptables rules + gateway service unit
- [ ] Config: `product.yaml` `web_gateway.enabled` flag

### Phase 2: Browser proxy (unblocks browser_tool)
- [ ] `tools/web_gateway/socks_proxy.py` — SOCKS5 mini-proxy
- [ ] `tools/browser_tool.py` — inject `--proxy-server` when `BROWSER_PROXY_URL` is set
- [ ] `hermes_cli/product_runtime_staging.py` — inject `BROWSER_PROXY_URL`

### Phase 3: Search backend on tailnet (no cloud API keys needed)
- [ ] Deploy SearXNG or Brave Search proxy on a tailnet peer
- [ ] `gateway_server.py`: route `/v1/search` to the tailnet search backend
- [ ] Documentation for operators

---

## Open Questions

1. **Tailscale exit node vs dedicated tailnet peer**: The exit node approach is simpler to operate but puts the GPU server "on the internet" (with Tailscale routing). A dedicated browsing peer is more isolated. Decision depends on your tailnet topology.

2. **agent-browser `--` passthrough**: We need to verify that `agent-browser >=0.13` forwards extra CLI args after `--` to Chromium. If not, the SOCKS5 proxy option may need to be injected via the `AGENT_BROWSER_EXTRA_ARGS` env var or a config file.

3. **Firecrawl SDK proxy support**: The Firecrawl Python SDK uses `requests` under the hood. When `FIRECRAWL_API_URL` is set to the gateway, the SDK sends requests to the gateway (not to the internet), so `HTTP_PROXY` env vars are irrelevant for the SDK itself — this is cleaner.

4. **gVisor netstack vs host network**: By default, gVisor containers use the netstack (userspace TCP/IP). If `--network=host` is ever used, gVisor's isolation weakens significantly. The product should never set `--network=host` for runtime containers (it doesn't currently).

5. **Rate limiting**: The gateway should enforce per-user rate limits to prevent an agent from burning through quota or DoS-ing external services.
