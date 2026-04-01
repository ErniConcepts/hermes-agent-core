# Hermes Core Feature Roadmap

This roadmap tracks the next plausible feature bets for the `hermes-core` fork.

Rules for items on this list:

- each roadmap item must have a corresponding spec file
- prefer Hermes-native behavior over product-only policy
- prefer narrow, testable increments over large platform rewrites
- keep the product web surface intentionally small

## Priority Order

### 1. Secure Browser Access

Spec:
- `docs/fork/spec-secure-browser-access.md`

Why first:
- it unlocks one of the biggest remaining runtime capability gaps
- it is primarily a security and infrastructure problem, not a UX problem
- it should be solved before broadening browser or web tooling

Status:
- researched
- not implemented

### 2. Intelligent Model Switching

Spec:
- `docs/fork/spec-intelligent-model-switching.md`

Why second:
- this fork already targets mixed local-model setups up to roughly 27B
- Hermes already contains a `smart_model_routing` foundation
- the right next step is to make that capability usable for the product runtime instead of inventing a parallel router

Status:
- researched
- not implemented

### 3. Multiple Agents Per User Runtime

Spec:
- `docs/fork/spec-multi-agent-runtime.md`

Why third:
- Hermes already has subagent delegation machinery
- the fork can likely reuse that instead of inventing a separate orchestration layer
- this becomes more valuable once model routing is clearer

Status:
- researched
- not implemented

### 4. LibreOffice or Similar Office Tool

Spec:
- `docs/fork/spec-office-tool.md`

Why fourth:
- useful, but less foundational than secure browser access or routing
- can be implemented as a narrow runtime tool without reshaping the platform
- should be designed around headless conversion/edit flows and strict workspace boundaries

Status:
- researched
- not implemented

## Decision Notes

- secure browser access should start as a minimal host-side egress capability, not a giant middleware system
- intelligent model switching should begin by extending Hermes's existing routing/config surfaces
- multi-agent work should reuse `delegate_task` and Hermes auxiliary model configuration where possible
- office tooling should favor a narrow, deterministic server-side integration over full desktop-in-a-container approaches

## Related Specs

- `docs/fork/spec-secure-browser-access.md`
- `docs/fork/spec-intelligent-model-switching.md`
- `docs/fork/spec-multi-agent-runtime.md`
- `docs/fork/spec-office-tool.md`
- `docs/fork/WEB-EGRESS-GATEWAY.md`
