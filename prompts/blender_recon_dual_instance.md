You are connected to TWO Blender instances, each exposed through its own Blender
MCP server. Before doing anything else, list and read the tools that EACH MCP
server provides, so you understand exactly what operations each instance supports.

## Blender instances

There are two MCP servers, on two different ports. The specific ports are supplied
through the MCP configuration file, so address each server by its NAME, not by a
hardcoded port:

* `blender-viewport-only` — holds the **GT (ground-truth) 3D model**. This
  instance is READ-ONLY: you can only take viewport screenshots and change the
  viewport / camera view. Use it to OBSERVE the GT from many angles by switching
  the viewport and capturing screenshots. It exposes no scene-editing tools, and
  any attempt to run geometry-modifying code there is rejected.
* `official-blender-mcp` — your WORKSPACE, served by Blender Foundation's
  official Blender MCP. It exposes the official inspection, documentation,
  screenshot, viewport/render, and full-access `execute_blender_code` tools.
  Here you EXECUTE the complete generated Blender Python script to build the
  **generated 3D model**.

## Objective

Observe the GT 3D model in `blender-viewport-only`, then procedurally build a
reconstruction in `official-blender-mcp` whose geometry is as close to the GT as
practical. The goal is a generated model that, viewed from the same angles, is
as close as possible to the GT.

Do not stop at a rough approximation. Repeatedly observe the GT, modify, and
rerun the Blender Python script in `official-blender-mcp` until the reconstruction
closely matches the GT.

## GT preservation

The GT lives in `blender-viewport-only` and must never be modified. That instance
is read-only by design: do not attempt to edit, delete, hide, move, rotate,
scale, rename, or otherwise alter any GT object, mesh, material, modifier,
hierarchy, collection, transform, or visibility setting, and do not run
scene-mutating code there. All construction happens in `official-blender-mcp`.

## Inspect the GT (in blender-viewport-only)

Inspect the GT through `blender-viewport-only` before writing the final script.
Switch the viewport / camera to observe from multiple viewpoints, including:

* front, rear, left, right;
* top and bottom;
* front and rear three-quarter views;
* close-ups of ambiguous or distinctive geometry.

From these views, determine:

* overall dimensions, orientation, and aspect ratio;
* major parts and part count;
* proportions and relative scales;
* positions and rotations;
* symmetry and repeated components;
* attachment relationships;
* silhouettes;
* curvature and profile changes;
* visible materials and colors.

Note: `blender-viewport-only` exposes only viewport screenshots and view/camera
changes — precise numeric measurement tools (scene info, bounding boxes) are NOT
available there, so infer GT proportions visually by comparing several angles.
Do not rely only on the initial viewport image.

## Procedural decomposition

Decompose the GT into coherent geometry units.

For each unit, determine its:

* name and geometry type;
* dimensions and shape parameters;
* local position and orientation;
* attachment target;
* symmetry or repetition relationship.

Use suitable Blender procedures such as primitives, custom meshes, `bmesh`,
curves, extrusions, swept profiles, bevels, subdivision, solidify, mirror,
array, booleans, or Geometry Nodes.

Use sufficiently detailed geometry for important shapes. Avoid crude primitives
when a more faithful procedural construction is practical.

## Reconstruction script (executed in official-blender-mcp)

Write and execute one self-contained Blender Python script in
`official-blender-mcp` via its `execute_blender_code` tool.

Create a collection named:

`VLM_RECONSTRUCTION`

All generated objects must be placed in this collection.

Create a root Empty named:

`recon__root`

Parent every generated geometry object to this root.

Use deterministic object names beginning with:

`recon__`

Examples:

* `recon__body`
* `recon__head`
* `recon__leg_left`
* `recon__antenna_right`

Construct the model in a coordinate frame aligned with the GT as you observed it,
and keep the reconstruction at that GT-aligned origin. There is no GT object in
`official-blender-mcp` to sit beside, so no comparison offset is needed — the GT
and the reconstruction live in separate Blender instances and are compared by
matching viewpoints across the two servers (see Iterative reconstruction).

## Independence from the GT

The generated geometry must be fully self-contained. The GT is in a different
Blender instance and is not reachable from `official-blender-mcp`, so the
reconstruction cannot and must not depend on any GT object or mesh datablock.

Build everything procedurally. Your only inputs from the GT are visual
observation and estimated numerical measurements. Small manually defined vertex
sets for individual procedural parts are allowed.

## Safe reruns

Store the complete final script in a Blender Text Editor text block named:

`reconstruct_gt.py`

in `official-blender-mcp`. The script must be safe to rerun.

At the beginning, remove only the previous generated collection:

```python
collection_name = "VLM_RECONSTRUCTION"

existing = bpy.data.collections.get(collection_name)
if existing is not None:
    for obj in list(existing.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(existing)
```

Never clear the full Blender scene or remove objects outside
`VLM_RECONSTRUCTION`.

## Materials and viewport

Create reconstruction materials when useful. They may resemble the GT colors you
observed in `blender-viewport-only`.

After generation, frame the reconstruction in the `official-blender-mcp` viewport
from angles that match the ones you used to observe the GT, so the two can be
compared screenshot to screenshot.

## Iterative reconstruction

After every script execution, compare the GT (screenshots from
`blender-viewport-only`) with the reconstruction (screenshots from
`official-blender-mcp`) from the SAME set of viewpoints.

Check:

* total dimensions and aspect ratio;
* part count;
* symmetry and repeated-part spacing;
* part size, position, and orientation;
* attachment and intersections;
* front, side, and top silhouettes;
* important curvature and distinctive local geometry.

Then:

1. identify the largest discrepancy;
2. modify `reconstruct_gt.py`;
3. rerun the complete script in `official-blender-mcp`;
4. re-screenshot both instances and inspect again;
5. repeat until no major visible discrepancy remains.

Prioritize corrections in this order:

1. missing or extra parts;
2. global dimensions and proportions;
3. part position, rotation, and scale;
4. attachment and floating geometry;
5. major silhouettes;
6. distinctive local shapes;
7. materials and minor details.

Do not declare completion while major parts, proportions, placements, repeated
structures, or silhouettes are clearly incorrect.

The final reconstruction should be as close as possible to the GT when compared
from matching viewpoints, except for intentional material differences and minor
details that are impractical to reproduce procedurally.

## Final scene requirements

At completion:

* `blender-viewport-only`: the GT model remains completely unchanged.
* `official-blender-mcp` must contain:
  * the `VLM_RECONSTRUCTION` collection;
  * the reconstructed objects;
  * the `recon__root` root Empty;
  * the `reconstruct_gt.py` text block.

Before finishing, verify that:

* the GT in `blender-viewport-only` remains unchanged;
* the script runs without errors in `official-blender-mcp`;
* the script can be safely rerun;
* only generated objects are removed during reruns;
* no major geometric or proportional discrepancy remains when comparing the two
  instances' screenshots.

## Final response

Report:

* the tools discovered on each MCP server (`official-blender-mcp` and
  `blender-viewport-only`);
* reconstruction collection name;
* Blender text-block name;
* generated object count;
* main geometry construction techniques;
* GT viewpoints inspected in `blender-viewport-only`;
* number of revision cycles;
* main corrections made;
* major remaining differences.

Do not claim completion unless the script has executed successfully in
`official-blender-mcp` and the reconstruction visually matches the GT observed in
`blender-viewport-only`.
