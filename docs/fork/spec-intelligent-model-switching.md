# Spec: Intelligent Model Switching

## Summary

Enable practical model switching for the product runtime by extending Hermes's existing smart-routing and auxiliary-model capabilities, rather than introducing a separate fork-only router.

## Problem

The fork targets mixed deployments:

- local models up to roughly 27B
- varying strengths across reasoning, tool use, speed, and multimodal support

One model is rarely best for everything:

- small local model for cheap or simple turns
- stronger model for harder turns or tool planning
- vision-capable model for image or document interpretation

## Goal

Let the product runtime choose a better model path automatically when that can be done safely and transparently.

## Non-Goals

- a large bespoke routing service in the fork
- black-box routing with no operator control
- per-message UI complexity about which model was used unless observability requires it

## Key Finding

I did not find a separate maintained Hermes fork that already solves this better than mainline. The important discovery is that Hermes core itself already contains routing substrate:

- `smart_model_routing` in `hermes_cli/config.py`
- `agent/smart_model_routing.py`
- gateway integration in `gateway/run.py`

So the most promising implementation path is to extend product runtime usage of that existing substrate instead of creating a second routing system.

## Recommended Direction

Phase 1 should be conservative:

1. reuse Hermes `smart_model_routing` config
2. allow product runtime to honor the same routing decision logic gateway already uses
3. keep routing limited to simple cases first:
   - cheap model for clearly simple turns
   - primary model for everything else
4. keep vision routing separate via existing auxiliary vision config

## Research Notes

Local code evidence:

- generic config already contains:
  - `smart_model_routing.enabled`
  - `smart_model_routing.max_simple_chars`
  - `smart_model_routing.max_simple_words`
  - `smart_model_routing.cheap_model`
- gateway already resolves turn routes through `agent.smart_model_routing.resolve_turn_route`

External reference:

- broader agent-routing projects exist, but they are heavier than what this fork needs:
  - https://github.com/eclipse-lmos/lmos-router

Conclusion:

- Hermes already has the right light-weight substrate
- the fork should reuse it instead of importing a complex new router

## Candidate Phase 1

- product runtime consults Hermes smart-routing on each turn
- if a cheap or simple route is selected, use that model for the turn
- otherwise use the normal primary model
- keep toolsets and runtime behavior unchanged

## Candidate Phase 2

- optional escalation rules for known-hard tasks
- optional specialist route for delegation or subagents
- explicit operator docs for recommended local-model pairs

## Main Risks

- switching too aggressively can confuse session continuity
- local-model providers may differ in tool-call quality
- model switching inside one persistent session may need careful transcript/provider handling

## Success Criteria

- simple turns are reliably offloaded to a cheaper or faster model when configured
- harder turns still use the primary model
- product runtime behavior stays understandable and testable
- routing stays configured from normal Hermes config, not a hidden product-only config source

## Test Plan

- unit tests for product runtime route resolution
- integration tests confirming cheap-vs-primary routing decisions
- live WSL validation with one simple-turn and one complex-turn prompt
