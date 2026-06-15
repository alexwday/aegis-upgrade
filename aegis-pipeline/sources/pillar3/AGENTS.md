# AGENTS.md

## Repository Context

- This workspace contains two parallel Python components: `database/` and `retrieval/`.
- Do not edit `venv/`, `.env` files, `.DS_Store`, generated `__pycache__/`, or runtime logs unless the user explicitly asks.
- Treat `data-input/` as user-provided source material. Do not rewrite, delete, or normalize it without confirmation.
- This directory may not be a Git repository. When possible, start Codex from `/Users/alexwday/Projects/aegis-pillar3` so this file is loaded.

## Working Agreements

- Read the relevant code before changing behavior. Prefer existing local patterns in `utils/`, `connections/`, and `main.py`.
- Keep changes narrow and reversible. Avoid broad refactors unless they are required for the requested task.
- When a task touches both `database/` and `retrieval/`, check whether the parallel implementation should stay aligned.
- For ambiguous or high-risk work, first produce a short plan that names the files, commands, data sources, and validation steps you expect to use.

## Safety And Side Effects

- Do not add production dependencies or make live network, database, NAS, OAuth, or OpenAI calls without confirming the expected side effects.
- Do not run commands that mutate external systems, upload/download private data, send messages, rotate credentials, or change remote state without explicit approval.
- Keep all work inside this workspace unless the user asks otherwise. Do not write outside `/Users/alexwday/Projects/aegis-pillar3` without approval.
- Never print, summarize, or expose secrets from `.env` files, logs, tokens, credentials, connection strings, cookies, or API responses.
- Prefer local, deterministic validation. For connector changes, use mocks, fixtures, dry-runs, or compile-time checks before any live service call.
- If a task requires internet access, explain why it is needed, what domains or tools are involved, and what data could leave the machine.

## Data And Financial Evidence

- Treat financial data, investor materials, retrieved evidence, and generated outputs as reviewable diligence artifacts.
- Do not invent metrics, dates, source labels, calculations, or management commentary. If support is missing or conflicting, say so.
- Cite the source file, worksheet/tab, section, or record behind every material financial claim when producing analysis or summaries.
- Separate confirmed facts from assumptions, estimates, and inferences. Preserve contradictions instead of silently reconciling them.
- Flag stale labels, unsupported variances, broken source links, missing owner inputs, and anything requiring finance-owner review.

## Comments And Docstrings

- Keep comments and docstrings aligned with the code in the same change. If behavior, inputs, outputs, side effects, errors, or configuration change, update adjacent documentation before finishing.
- Add or update docstrings for public modules, classes, functions, connector methods, config helpers, and non-obvious data transformations.
- Docstrings should describe the contract: purpose, important arguments, return shape, raised errors or fallback behavior, required environment variables, and external side effects.
- Use comments to explain why a non-obvious decision exists, not to restate the next line of code.
- Remove stale comments, misleading TODOs, and commented-out code. If a TODO remains, include the concrete condition that would make it actionable.
- Do not claim guarantees in docs that tests or code do not enforce. Prefer explicit uncertainty over over-documenting behavior.

## Quality Gates

- Run the narrowest useful validation after code changes. At minimum for Python edits, run:

  ```bash
  venv/bin/python -m compileall database retrieval
  ```

- If tests are added or discovered, run the targeted test first, then the broader suite when risk justifies it:

  ```bash
  venv/bin/python -m pytest
  ```

- If project linting or formatting config is introduced later, run the configured tools before finishing. Do not invent lint commands that are not installed or configured.
- For connector changes, prefer deterministic smoke checks with mocked credentials or fixtures. Do not require live services unless the user approves.
- Before reporting completion, state which validation commands ran and any commands that could not be run.

## Done Means

- The requested behavior or artifact is complete, scoped to the user request, and does not include unrelated cleanup.
- Comments, docstrings, and adjacent documentation match the implemented behavior.
- The narrowest useful validation has run, or the reason it could not run is stated.
- Known remaining risks, unsupported assumptions, missing evidence, and follow-up items are called out clearly.
- For file edits, the final response names the changed files and the validation performed.

## Review Standard

- Review for behavioral regressions, secret exposure, resource leaks, missing validation, stale comments/docstrings, and untested edge cases.
- For data or retrieval changes, preserve uncertainty and provenance. Do not silently infer unsupported facts from incomplete source material.
- For generated reports, decks, spreadsheets, or memos, review source traceability, calculation support, stale language, and places where reviewer judgment is required.
- Keep final responses concise: summarize what changed, what was validated, and any remaining risk.
