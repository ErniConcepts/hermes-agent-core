# gVisor Experiment

This directory holds the smallest viable experiment for MYNAH's runtime-isolation decision.

The goal is to answer one question early:

Can Hermes run inside an isolated container runtime such as `gVisor` without significant source changes?

## Scope

This is not a production container image.

It is only a compatibility harness that:
- builds Hermes and `mini-swe-agent` into one Linux image
- runs a non-interactive smoke test
- gives us one image we can run under standard Docker first and `gVisor` second

## What the smoke test checks

- Hermes imports successfully
- the CLI entrypoint is available
- the Docker terminal backend test suite can run inside the image
- file-backed memory under `HERMES_HOME` works normally

This is intentionally narrow. We only want to prove runtime compatibility first.

## Prerequisites

### Host

- Docker installed and running
- Linux container support enabled
- `mini-swe-agent` submodule initialized:

```bash
git submodule update --init mini-swe-agent
```

### For the gVisor pass

- `gVisor` installed
- `runsc` available on the host
- Docker configured with a `runsc` runtime

Typical Docker daemon registration:

```json
{
  "runtimes": {
    "runsc": {
      "path": "runsc"
    }
  }
}
```

## Build

From the repo root:

```bash
docker build -f experiments/gvisor/Dockerfile -t hermes-gvisor-smoke .
```

## Run baseline under standard Docker

```bash
docker run --rm hermes-gvisor-smoke
```

Expected result:
- the smoke test prints environment details
- Hermes imports cleanly
- targeted tests pass
- `MEMORY.md` and `USER.md` are written under `/var/lib/hermes`

## Run under gVisor

```bash
docker run --rm --runtime=runsc hermes-gvisor-smoke
```

## Run the MYNAH lockdown verification

This verifies the first MYNAH fork patch:

- `MYNAH_PRODUCTION_MODE`
- no plugin/MCP discovery
- explicit MYNAH-only toolsets
- local inference still works

Standard Docker:

```bash
docker run --rm \
  -e MYNAH_PRODUCTION_MODE=1 \
  -e MYNAH_TEST_BASE_URL=http://host.docker.internal:8080/v1 \
  -e MYNAH_TEST_MODEL=qwen3.5-9b-local \
  hermes-gvisor-smoke \
  python experiments/gvisor/verify_mynah_lockdown.py
```

gVisor:

```bash
docker run --rm --runtime=runsc \
  -e MYNAH_PRODUCTION_MODE=1 \
  -e MYNAH_TEST_BASE_URL=http://host.docker.internal:8080/v1 \
  -e MYNAH_TEST_MODEL=qwen3.5-9b-local \
  hermes-gvisor-smoke \
  python experiments/gvisor/verify_mynah_lockdown.py
```

## Success criteria

- same image works under both `runc` and `runsc`
- no Hermes source changes are required for basic runtime startup
- no major filesystem, process, or dependency incompatibilities appear

## If gVisor fails

We keep the same image shape and fall back to plain containers first.

That still preserves the intended MYNAH architecture:
- one isolated Hermes-derived runtime per user
- shared inference backend
- control plane outside the runtime

## Next steps after a successful run

1. Add a narrower MYNAH-safe tool profile.
2. Run a real agent-loop smoke test against a local OpenAI-compatible endpoint.
3. Test per-user namespaced `HERMES_HOME` mounts.
4. Add a second image/profile for production-safe runtime settings.
