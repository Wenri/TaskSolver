# Task skills

Skills in this directory are read by **both** agy and codex. Each is a directory containing a
`SKILL.md`: YAML frontmatter with `name` and `description` (the only fields either CLI reads),
followed by markdown instructions, optionally with `references/` files reached by relative links.

Both CLIs discover `.agents/skills/` by walking up from the working directory to the repo root, so
anything committed here is live for any run whose cwd is under this repo — no configuration.

Both load skills **lazily**. Only each skill's name and description sit in context; the body is read
when the model decides to use it, and reference files only when the body sends it there. That is the
whole reason a long task prompt belongs here rather than in an always-on `AGENTS.md`.

## `blender-gt-reconstruction`

Reconstructs a ground-truth 3D model through a Blender MCP server: inspect the GT, then build a
procedural, rerunnable Blender Python reconstruction that matches it.

```
blender-gt-reconstruction/
├── SKILL.md                     237 lines   mode-agnostic core + routing
└── references/
    ├── same-scene.md             73 lines   GT lives in this Blender
    ├── two-instance.md           74 lines   GT lives behind a read-only server
    └── textures.md              118 lines   stage 2
```

What each layer costs in context — the point of the split:

| layer | loads | size |
| --- | --- | --- |
| `name` + `description` | always — one catalog line | ~950 chars |
| `SKILL.md` body | when the skill is selected | 237 lines |
| one access-mode reference | after Step 0 routes | ~74 lines |
| `textures.md` | only if geometry passes stage 1 | 118 lines |

This replaced two standalone prompts of 333 and 241 lines that shared 11 of 13 sections and had to
be kept in sync by hand. Now a turn that is not about Blender costs one line.

### How `SKILL.md` is organized

```
# Blender GT reconstruction
## Step 0 — determine access mode and scope    routes to a reference
## GT preservation          (principle only)
## Inspect the GT           (viewpoints + what to determine)
## Procedural decomposition
## Reconstruction script    (VLM_RECONSTRUCTION / recon__root / recon__ names)
## Independence from the GT (principle only)
## Safe reruns              (the rerun-safe deletion snippet)
## Iterative reconstruction (checklist + 7-step priority)
## Final scene requirements
## Final response
```

Everything mode-specific is pushed down into a reference, so the output contract is stated exactly
once. Step 0 routes by **capability, not server name** — names come from MCP config and can be
renamed — and an explicit statement in the task overrides the inference.

Each reference opens with a guard line, so reading the wrong one self-corrects. The failures that
matter are applying the side-by-side offset in two-instance mode, or running scene-modifying code
against the read-only GT server.

The two access-mode references are deliberately parallel: same seven headings, opposite answers.

| | same-scene | two-instance |
| --- | --- | --- |
| Inspecting | exact bounding boxes, transforms, sampled colors | screenshots only; infer proportions visually |
| Placement | offset `1.25 * max(bbox)`, prefer world X | origin, no offset (it is uncomputable) |
| Independence | a real ban list (booleans, bake, datablock reuse) | satisfied by construction |
| Comparing | both models in one viewport | screenshot-to-screenshot at matching viewpoints |

Scope is the second axis: geometry is always stage 1, and textures are stage 2, gated on geometry
passing its checks. Full scope is the default in both modes.

## Usage

Inside this repo, nothing is needed — both CLIs already find it.

For a throwaway workspace, seed it and pass that workspace along:

```python
from wirecap.runtime.workspace import ensure_git_workspace
import pycodex, pyagy

ws = ensure_git_workspace(skills=[".agents/skills/blender-gt-reconstruction"])
pycodex.ask("Reconstruct the GT model.", workspace=ws)
pyagy.ask("Reconstruct the GT model.", workspace=ws)      # same workspace, same skill
```

Scope a run — geometry only — through always-on instructions rather than by editing the skill:

```python
ws = ensure_git_workspace(
    skills=[".agents/skills/blender-gt-reconstruction"],
    instructions="Task: reconstruct the GT per the blender-gt-reconstruction skill. Scope: geometry only.")
```

Invoke it explicitly instead of relying on the description matching (codex uses a `$` sigil):

```python
pycodex.ask("$blender-gt-reconstruction — rebuild the model in the scene.", workspace=ws)
```

**Pass `workspace=ws`.** A bare `ask(prompt)` resolves the shared scratch repo with no seeds, which
*clears* whatever was just seeded there.

## Verifying that a skill actually reached the model

The capture pipeline can prove it without spending model credits: `codex_request` is emitted before
the HTTP call, so the request body records whatever catalog was injected even if the turn then
fails. Raise `WIRE_PREVIEW` so the record keeps the whole body rather than the default 64-byte head,
and read the capture JSONL.

Two things to know when doing this: a run emits **two** `codex_request` records and only the second
carries the workspace context, and the `head` field is hex-encoded. Confirmed this way on codex
0.146.0, where the skill is rendered into the catalog beside codex's own bundled skills:

```
- blender-gt-reconstruction: Reconstruct, rebuild, replicate, or visually match a
  ground-truth (GT) 3D model that already exists in a connected Blender MCP instance... (file: ...)
```

## Adding a skill

Create `<name>/SKILL.md` with frontmatter of exactly `name` and `description` — other fields
(`license`, `allowed-tools`, `version`) are dead weight to both loaders. Keep `name` hyphen-case and
under 64 characters, matching the directory name. The `description` is the only selection signal and
is capped at 1024 characters, so lead with trigger phrasing and end with what the skill is *not*
for. Keep the body under ~500 lines and push bulk into `references/`, one level deep.

Validate against codex's own rules with the checker it ships at
`codex/vendor/codex-rs/skills/src/assets/samples/skill-creator/scripts/quick_validate.py` (it needs
PyYAML, which the pixi env does not carry). Do not add a `README.md` inside a skill directory —
codex's skill-creator guidance lists it as an anti-pattern; document skills here instead.
