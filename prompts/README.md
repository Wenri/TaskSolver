# prompts — task prompts for agentic CLIs driving Blender MCP

Prompt templates handed to an agentic coding CLI (Claude Code, `agy`, `codex` — see
`tasksolver/claude_code.py`, `antigravity/pyagy`, `codex/pycodex`) that has a **Blender MCP**
server connected. Each file **is** the prompt, verbatim and self-contained: pipe or paste the whole
file with no editing.

The task is BlenderAlchemy-style GT reconstruction — observe a ground-truth 3D model, then write and
iteratively refine one Blender Python script that procedurally rebuilds it, never touching the GT.

## The two variants

| file | GT lives in | comparison | GT preserved by |
| --- | --- | --- | --- |
| [`blender_recon_single_instance.md`](blender_recon_single_instance.md) | the **same** scene as the reconstruction | side by side in one scene, offset along world X by `1.25 * max(gt_bbox_dimensions)` | discipline — the prompt forbids touching GT datablocks |
| [`blender_recon_dual_instance.md`](blender_recon_dual_instance.md) | a **second, read-only** Blender instance (`blender-viewport-only`) | screenshot-to-screenshot across the two servers, from matching viewpoints | the server itself — it exposes no editing tools |

Pick **single-instance** when one Blender holds the GT: the model can take exact measurements
(bounding boxes, transforms, sampled colors), and the reviewer sees both models in one viewport. It
also runs the fuller task — geometry **and then** texture/material reconstruction, in that order.

Pick **dual-instance** when the GT must be tamper-proof, or when you want to measure how well a model
reconstructs from **vision alone**. `blender-viewport-only` offers only screenshots and view changes,
so no numeric measurement is possible and proportions must be inferred visually. Scope is geometry;
materials are optional. Address the servers by **name**, not port — ports come from the MCP config.

## Serving one as the run's standing instructions

Both CLIs discover an `AGENTS.md` beside the working directory and apply it for the whole session
(agy walks up from the cwd to the repo root loading `GEMINI.md`/`AGENTS.md`; codex reads `AGENTS.md`
plus an `AGENTS.override.md`). So a template is best delivered as the workspace's standing
instructions rather than pasted into every turn — `ensure_git_workspace` seeds it, which is the one
hook both wrappers already share:

```python
from wirecap.runtime.workspace import ensure_git_workspace

task = open("prompts/blender_recon_single_instance.md").read()
ws = ensure_git_workspace(instructions=task)      # writes AGENTS.md into the workspace

import pycodex; pycodex.ask("Reconstruct the GT model.", workspace=ws)
import pyagy;   pyagy.ask("Reconstruct the GT model.", workspace=ws)
```

The per-turn prompt then stays short, and the task survives across turns of an interactive session.
The scratch repo is reused between runs, so its `AGENTS.md` always reflects the latest call —
passing no `instructions` clears it instead of inheriting the previous run's task. A workspace you
supply yourself is only ever written to, never stripped of its own `AGENTS.md`.

## Shared output contract

Both variants produce the same artifacts, which is what makes runs comparable across prompts, models,
and backends:

- collection `VLM_RECONSTRUCTION` — holds every generated object;
- root Empty `recon__root` — parent of all generated geometry (and, single-instance only, the
  comparison offset);
- `recon__`-prefixed names for all generated objects, materials, textures, images, and node groups;
- a Blender Text Editor text block `reconstruct_gt.py` holding the complete final script, safe to
  rerun — it deletes only `VLM_RECONSTRUCTION` and `recon__`-owned data, never the whole scene;
- a final report (revision-cycle counts, corrections made, remaining differences) — the per-run
  record for grading a reconstruction.

Both also forbid deriving the reconstruction from GT datablocks (no duplicating, importing, baking,
or using GT objects as boolean/shrinkwrap/Geometry Nodes inputs). Inspection and measurement are
allowed; the geometry and shading must be built procedurally.
