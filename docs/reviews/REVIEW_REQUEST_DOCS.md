# Documentation Review Request — README + QUICK_START (root and package)

Branch: https://github.com/grahama1970/scillm/tree/feat/codeworld-provider

Files to review
- README.md (root)
- QUICK_START.md (root)
- litellm/README.md
- litellm/QUICK_START.md

Goals
- Make the value prop obvious to scientists/engineers/mathematicians in 30 seconds.
- Verify paths reflect the new layout (deploy/docker/*, local/artifacts/logo/*). No broken image/compose links.
- Keep usage scenarios concrete with copy/paste commands that lead to green runs.

Context (Perplexity MCP review highlights)
- Differentiator: specialized infrastructure for theorem proving, formal code automation, and experiment tracking.
- Overlap: LeanDojo/tooling; generic LLM eval frameworks (DeepEval/Langfuse/OpenAI Evals); agent shims (LangChain/Open Interpreter).
- Scenarios: strong; consider examples for automated agent curricula and failure-analysis pipelines.

Requested edits (examples OK to tweak)
- In README (root), near the top:
  “Unlike generic LLM frameworks, SciLLM provides specialized infrastructure for theorem proving, formal code automation, and experiment tracking—ideal for benchmarking proof‑aware agents, integrating with formal math libraries, and prototyping prove‑aware research tools efficiently.”
- In QUICK_START (root), include a “More Use Cases” section (added) with:
  - Automated curriculum generation for agents
  - Failure analysis pipelines (extract unproved goals + diagnostics)
  - Headless local loop in CI (gate on % proved/judge thresholds; publish artifacts)

What to check
- Compose/docs/signpost consistency (deploy/docker vs local/docker notes)
- Image paths (../local/artifacts/logo/* in litellm/README.md)
- TL;DR blocks are runnable and minimal

Please reply with unified diffs or direct line edits. Thanks!
