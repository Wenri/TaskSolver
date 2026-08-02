# Same-scene mode — the GT lives in the Blender you are building in

**You are in this mode only if** a single Blender MCP server holds both the GT model and your
reconstruction, and you can execute Python in it. If instead the GT sits behind a separate
read-only viewport server, stop and read [two-instance.md](two-instance.md).

The final scene must contain **both** the unchanged GT model and the generated reconstruction,
visible side by side.

## GT preservation in this mode

The GT is reachable and editable here, so preservation is a discipline you must enforce.

**Before running generated code, identify and record all existing GT objects and collections.** The
reconstruction script may only create, remove, or modify its own generated objects, materials,
textures, images, node groups, and collection.

Preserve all GT materials, textures, images, UV data, and shader settings unchanged.

## Inspecting the GT

Exact measurement is available — use it rather than guessing:

* world-space bounding boxes and object dimensions;
* transforms, distances, angles, symmetry planes;
* mesh statistics;
* sampled colors and material observations.

Take these alongside the multi-viewpoint visual inspection in SKILL.md.

## Placement — the comparison offset

Construct the model in the GT-aligned frame, then translate **only `recon__root`** for side-by-side
comparison. Compute the distance from the GT world-space bounding box:

```python
comparison_distance = 1.25 * max(gt_bbox_dimensions)
```

Prefer translation along world X. Use another axis only when X causes overlap or poor visibility.

## Independence — what is reachable here, and therefore banned

Because GT datablocks are reachable in this scene, the reconstruction must not:

* duplicate GT objects or mesh datablocks;
* import or append the source model;
* copy or serialize the complete GT vertex and face arrays;
* use GT objects as boolean, shrinkwrap, Geometry Nodes, constraint, driver, or instancing inputs;
* construct the reconstruction from evaluated GT meshes;
* duplicate or reuse GT materials, node trees, textures, images, or UV layers;
* extract or bake GT textures into reconstruction assets;
* reference GT materials, textures, images, or objects from generated shader nodes.

Inspection and numerical measurement remain allowed, as do manually observed colors, material
properties, texture frequencies, and pattern dimensions.

## Comparing

After each run, frame both models in the viewport and keep them visible simultaneously from a
useful comparison angle. Compare them directly, viewpoint by viewpoint.

## Final state for this mode

In addition to the SKILL.md checklist:

* both models are visible side by side;
* the GT model is present and unchanged;
* `recon__root` carries the comparison offset.

## Also report

* comparison offset and axis.
