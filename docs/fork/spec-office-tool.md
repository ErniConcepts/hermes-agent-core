# Spec: LibreOffice or Similar Office Tool

## Summary

Add a narrow office-document tool for product runtimes, likely using headless LibreOffice or a compatible server-side document engine, to support common document conversions and basic office-file workflows inside the user workspace.

## Problem

The current runtime handles text/code/files well, but common office workflows are weak:

- `.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`, `.odp` are common user artifacts
- local models often need deterministic conversion or export help
- terminal-only handling of office files is limited and brittle

## Goal

Support a small, deterministic subset of office workflows:

- convert office documents to PDF or plain text
- extract readable text for model reasoning
- optionally update or generate simple office documents through controlled conversion flows

## Non-Goals

- full interactive desktop office editing
- exposing a GUI office suite in the runtime
- broad macro execution
- giving the agent arbitrary access outside `/workspace`

## Recommended Direction

Start with a conversion/extraction tool, not editing.

Phase 1:

- use headless LibreOffice if available
- restrict input/output to `/workspace` and `/workspace/.tmp`
- allow conversions like:
  - document to PDF
  - document to text/markdown where reliable
- expose a narrow JSON-returning Hermes tool

## Why this direction

Headless LibreOffice already supports command-line conversion and server-style execution patterns. That makes it a better fit than trying to embed a full office GUI into the runtime.

## Research Notes

LibreOffice command-line/headless support:

- LibreOffice documents command-line parameters including `--headless` and `--convert-to`:
  - https://help.libreoffice.org/latest/bs/text/shared/guide/start_parameters.html
- LibreOffice documents that headless usage on Linux is supported, though binaries still need required libraries:
  - https://wiki.documentfoundation.org/Development/HeadlessBuild

Important operational note from LibreOffice docs:

- LibreOffice requires write access to its user profile directory
- this means the tool must isolate its LibreOffice profile inside runtime-internal writable storage, not in a shared global location

## Architecture Constraints

- tool must operate entirely inside the existing per-user runtime
- writable scratch/profile state should live in `/workspace/.tmp`
- user-visible outputs should be placed in `/workspace`
- avoid Java/UNO complexity in phase one unless conversion fidelity forces it

## Candidate Tool Shape

Examples:

- `office_convert`
- `office_extract_text`

Likely parameters:

- `input_path`
- `output_format`
- optional `output_path`

## Open Questions

- whether LibreOffice should be baked into the runtime image or installed as an optional variant
- whether PDF export alone is enough for phase one
- whether document extraction should use LibreOffice directly or a separate lightweight extractor for some formats

## Success Criteria

- deterministic conversion of common office files inside the runtime
- no writes outside runtime-safe paths
- outputs appear in the user workspace when requested
- tool failures are explicit and inspectable

## Test Plan

- unit tests for path validation
- runtime image/build test for LibreOffice presence if bundled
- integration tests with fixture files:
  - `.docx`
  - `.xlsx`
  - `.pptx`
- verify output goes to `/workspace` while temp/profile state stays under `/workspace/.tmp`
