# Clarification Skill

## Purpose

This skill provides a short, actionable template and guidance for agents to follow when a user's request is ambiguous, underspecified, or may involve unilateral design decisions. It is intentionally minimal and designed to be linked from `AGENTS.md`.

## Rules for agents

- If any requirement, constraint, or desired behavior is ambiguous or missing, ask one or more concise clarifying questions before taking non-trivial action.
- Do not make unilateral design or policy decisions unless the user explicitly grants permission or sets a default.
- When presenting options, keep them short (2–4 choices), list trade-offs, and ask the user to pick one.
- If the user grants a default or permission to choose, proceed and explicitly state the chosen default in the reply.

## Clarification templates

Use these short prompts when asking the user to clarify:

- "Do you want X or Y? If unsure, I can pick X (safer) or Y (faster). Which do you prefer?"
- "I can proceed with option A (keeps current behavior) or B (breaking change). Which should I use?"
- "Which target environment should I assume: local Docker (recommended) or remote server?"

## Quick usage example

1. User: "Add logging to the service."
2. Agent: "Do you want structured JSON logs or simple text logs? JSON (better parsing) or text (easier to read)?"

## Where to use

- Link from `AGENTS.md` as the canonical clarification guidance for all agents operating in this repository.
- Agents may embed short variants of these templates into interactive prompts when requesting input from the user.
