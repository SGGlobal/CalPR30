# Skippy Architecture

## Philosophy

> Skippy owns the architecture.

Models are replaceable.
Tools are replaceable.
Providers are replaceable.

Skippy remains the constant.

---

## System Architecture

                    Skippy
                       │
        ┌──────────────┼──────────────┐
        │              │              │
     Brain          Memory        Learning
        │              │              │
        └──────────────┼──────────────┘
                       │
                    Hermes
                       │
                Reasoning Engine
                       │
        ┌──────────────┼──────────────┐
        │              │              │
    Polymarket      RimWorld      Albion
        │
     OpenClaw
     AutoIt
     Obsidian

---

## Core Principles

1. One responsibility per module.

2. Brain coordinates.

3. Hermes reasons.

4. Memory stores experience.

5. Learning improves future decisions.

6. Adapters connect external systems.

7. Models are replaceable.

8. Skippy owns the architecture.

9. Intelligence emerges from experience.

10. Every decision must be explainable.

11. Local-first. Cloud only when it adds value.

---

## Cognitive Loop

Observe

↓

Recall

↓

Reason

↓

Decide

↓

Remember

↓

Learn

---

## Runtime

Default Runtime

- Ollama

Supported Models

- Qwen
- Gemma
- Llama
- Future models

The runtime may change without affecting Skippy.

---

## Adapters

Current

- Polymarket

Planned

- RimWorld
- Albion
- Pocket Option
- SGGlobal
- Desktop Automation

All adapters communicate through the Brain.
