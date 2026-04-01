# Spec: Secure Browser Access

## Summary

Provide a secure, product-compatible way for per-user runtimes to access the web for browser-driven tasks without granting broad, uncontrolled internet egress from gVisor runtime containers.

This should solve browser access first. General web-search and extraction compatibility can follow, but browser access is the primary contract for this spec.

## Problem

Current product runtimes are intentionally constrained:

- per-user runtime containers run under `runsc` / gVisor
- product deployment is Tailnet-only
- direct container internet egress is either blocked or undesirable from a security perspective

That leaves the browser tool in an awkward state:

- technically available in Hermes
- operationally unsafe or non-functional in the product environment

## Goal

Allow runtime containers to browse external sites through a narrow, auditable, policy-controlled egress path.

## Non-Goals

- building a full browser farm
- cloning the entire Firecrawl API surface in phase one
- enabling unrestricted outbound internet from runtime containers
- broadening product runtime privileges outside current workspace/runtime boundaries

## Proposed Direction

Phase 1 should be minimal:

1. run a host-side egress service or proxy
2. bind it only where runtime containers can reach it
3. require explicit auth from runtime containers
4. log outbound requests with user/runtime attribution
5. enforce allow/block policy centrally
6. point the browser tool at that proxy path

The runtime container should never talk directly to the internet.

## Why This Direction

This matches the current product architecture better than trying to loosen container networking:

- gVisor runtime stays narrow
- Tailnet-only posture remains intact
- policy and auditing stay outside the user container

## Architecture Constraints

- keep per-user runtime containers as they are
- do not introduce a broad new product config console
- prefer an operator-owned host-side service over runtime-side hacks
- keep browser routing explicit and testable

## Possible Implementation Shape

- host-side `web-egress-gateway` service
- runtime env injection for browser proxy settings
- product runtime staging adds only the minimum env/config required
- browser tool consumes that route without product-specific tool logic if possible

## Research Notes

What supports this design:

- Playwright supports explicit proxy configuration at browser or browser-context level:
  - https://playwright.dev/python/docs/network
- Playwright's Docker guidance warns that browsing untrusted sites needs strong isolation:
  - https://playwright.dev/docs/docker
- existing fork design exploration already identified a host-side gateway as the right boundary:
  - `docs/fork/WEB-EGRESS-GATEWAY.md`

## Open Questions

- should phase 1 support only browser traffic, or browser plus plain HTTP fetches
- should policy be allowlist-first or denylist-first
- should the gateway be product-managed or operator-managed

## Success Criteria

- a runtime can open external sites through the approved path
- direct internet egress from runtime containers remains blocked
- outbound access is attributable to user/runtime identity
- browser E2E can verify one external navigation path safely in a controlled environment

## Test Plan

- runtime staging test for proxy env injection
- host-side gateway health test
- browser tool integration test against a controlled target URL
- live WSL or Linux validation with explicit cleanup
