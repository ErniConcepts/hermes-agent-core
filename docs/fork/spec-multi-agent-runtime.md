# Spec: Multiple Agents Per User Runtime

## Summary

Enable controlled multi-agent execution inside one user’s product runtime by reusing Hermes delegation and auxiliary model capabilities, rather than inventing a separate product-only orchestration system.

## Problem

Some user tasks are naturally multi-step or multi-perspective:

- planning plus execution
- code work plus document or image inspection
- main model plus specialist vision/model assistance

The fork already supports one runtime per user, but not an explicit product-level story for “multiple agents per user” beyond whatever Hermes delegation can do implicitly.

## Goal

Support bounded, understandable multi-agent work within one user runtime.

## Non-Goals

- multi-user shared agent swarms
- open-ended recursive agent trees
- exposing a complex orchestration UI in the product web app

## Recommended Direction

Reuse existing Hermes primitives first:

- `delegate_task` for child-agent execution
- auxiliary model configuration for specialist tasks
- existing tool restrictions for child agents

Phase 1 should be runtime-internal, not a UI feature:

- allow the main runtime agent to delegate in controlled cases
- optionally route some delegated tasks to a different configured model
- keep results summarized back into the main conversation

## Research Notes

The current codebase already has strong substrate:

- Hermes delegation is implemented in `tools/delegate_tool.py`
- child agents already get isolated context and restricted toolsets
- Hermes already has auxiliary model configuration for vision and related side tasks in `config.py`
- Playwright documentation also emphasizes multiple browser contexts/users as a normal orchestration pattern, which is conceptually useful for future multi-agent browser testing:
  - https://playwright.dev/

## Architectural Principle

Do not build a second orchestration framework in `product_*`.

Instead:

- define what multi-agent behavior is allowed in product runtimes
- configure and restrict it
- let Hermes own the child-agent loop

## Candidate Phase 1

- enable per-user runtime delegation only when explicitly configured
- keep default concurrency low
- allow a separate specialist model for delegated tasks if Hermes config already supports it
- document that a vision-capable auxiliary model can act as the “specialist” path for some workloads

## Main Risks

- local 9B-27B models may degrade badly if agent trees are too deep
- too much concurrency can amplify runtime instability
- web UI may need clearer explanation of what happened if subagent work becomes visible

## Success Criteria

- one user runtime can complete bounded delegated subtasks reliably
- child-agent toolsets remain narrower than or equal to the parent’s allowed surface
- the product does not need a new heavy orchestration UI to support it

## Test Plan

- unit tests around product runtime toolset/delegation policy
- integration tests for one delegated task in product runtime
- optional live E2E with a deterministic delegated task and visible final summary
