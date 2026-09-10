# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Solo home DJs using a local browser console to practice, learn, prepare, record, and perform mixes. The console also supports live-performance workflows when reliable local audio hardware is available.

## Product Purpose

Pulse DJ Console turns a personal music library and DJ knowledge base into a playable two-deck console with musical analysis, phrase-aware transition suggestions, recording, and guided practice.

## Positioning

It combines a real Web Audio DJ console with knowledge-base-grounded transition recommendations, rather than treating AI advice as a detached offline mix renderer.

## Operating Context

The product runs locally in Chromium-class browsers with local tracks, a FastAPI backend, the Music Brain analyzer, and the repository's Obsidian DJ knowledge base.

## Capabilities and Constraints

- Local-file-first, personal and educational use; no accounts, streaming catalogue, or paywall.
- Music ingestion, performance controls, and recordings operate locally.
- User-provided YouTube URLs may only be used for material the user is authorized to download and use.
- Audio functionality must degrade clearly when browser APIs or optional DSP capabilities are unavailable.

## Brand Commitments

Preserve the established Pulse DJ Console visual identity and its tactile, hardware-inspired operating controls.

## Evidence on Hand

The existing console, Music Brain package, transition cookbook in `DJ/`, automated Python tests, and local audio downloader are working product evidence.

## Product Principles

- Ground automated decisions in DJ theory, phrasing, and frequency ownership.
- Never represent an unavailable DSP or stem feature as active.
- Keep performance controls immediate, visible, and interruptible by the DJ.
- Prefer reliable local workflows over product-growth features.

## Accessibility & Inclusion

New controls remain keyboard-operable, expose clear labels and live status, retain visible focus, and meet WCAG AA text contrast where the existing visual system permits.
