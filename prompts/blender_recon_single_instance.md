Use the connected Blender MCP.

## Objective

Inspect the GT 3D model currently present in the Blender scene and create a procedurally generated reconstruction that is geometrically as close to it as practical.

The final scene must contain both:

1. the unchanged GT model;
2. the generated reconstruction.

Do not stop at a rough approximation. Repeatedly inspect, modify, and rerun the Blender Python script until the reconstruction closely matches the GT.

## Reconstruction priority

Reconstruct the model in two ordered stages:

1. **Geometry reconstruction**
2. **Texture and material reconstruction**

Complete and verify the geometry reconstruction before focusing on textures, materials, shaders, or other surface appearance.

During the geometry stage, prioritize:

1. missing or extra parts;
2. global dimensions and proportions;
3. part positions, rotations, and scales;
4. attachment relationships and intersections;
5. front, rear, side, top, and bottom silhouettes;
6. curvature, profile transitions, and distinctive local geometry;
7. topology or modifier details that materially affect the visible shape.

Do not spend significant effort matching textures or materials while major geometric discrepancies remain.

After the geometry is sufficiently close to the GT, inspect and reconstruct the visible texture and material characteristics. Match practical properties such as:

* base colors;
* color separation between parts;
* roughness and gloss;
* metallic or dielectric appearance;
* transparency or emission;
* visible texture patterns;
* normal or bump details;
* material assignments and boundaries.

Texture reconstruction must remain independent from the GT. Do not copy, duplicate, extract, bake, serialize, or directly reuse GT texture images, material node trees, UV data, or material datablocks. Recreate the visible appearance procedurally or with newly generated reconstruction assets.

Reconstruction materials should closely resemble the GT after geometry validation, while remaining slightly distinguishable where useful for side-by-side comparison.

## GT preservation

Do not modify the GT in any way.

Do not delete, hide, duplicate, rename, move, rotate, scale, join, replace, or edit any GT object, mesh, material, modifier, hierarchy, collection, transform, or visibility setting.

Before running generated code, identify and record all existing GT objects and collections. The reconstruction script may only create, remove, or modify its own generated objects, materials, textures, images, node groups, and collection.

## Inspect the GT

Inspect the GT through Blender MCP before writing the final script.

Use multiple viewpoints, including:

* front and rear;
* left and right;
* top and bottom;
* front and rear three-quarter views;
* close-ups of ambiguous or distinctive geometry;
* close-ups of important texture, material, and color boundaries after geometry reconstruction is validated.

Determine:

* overall dimensions and orientation;
* major parts and part count;
* proportions and relative scales;
* positions and rotations;
* symmetry and repeated components;
* attachment relationships;
* silhouettes;
* curvature and profile changes;
* visible materials and colors;
* surface finish and reflectivity;
* visible texture patterns and material boundaries.

Use Blender measurements where useful, including world-space bounding boxes, object dimensions, transforms, distances, angles, symmetry planes, mesh statistics, sampled colors, and material observations.

Do not rely only on the initial viewport image.

## Procedural decomposition

Decompose the GT into coherent geometry units.

For each unit, determine its:

* name and geometry type;
* dimensions and shape parameters;
* local position and orientation;
* attachment target;
* symmetry or repetition relationship;
* visible material category;
* texture or surface characteristics when relevant.

Use suitable Blender procedures such as primitives, custom meshes, `bmesh`, curves, extrusions, swept profiles, bevels, subdivision, solidify, mirror, array, booleans, or Geometry Nodes.

Use sufficiently detailed geometry for important shapes. Avoid crude primitives when a more faithful procedural construction is practical.

## Reconstruction script

Write and execute one self-contained Blender Python script.

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

Use deterministic names for generated reconstruction materials, textures, images, and node groups beginning with:

`recon__`

Construct the model in a coordinate frame aligned with the GT. Keep all part coordinates in this GT-aligned frame.

After construction, translate only `recon__root` for side-by-side comparison.

Compute the comparison distance from the GT world-space bounding box:

```python
comparison_distance = 1.25 * max(gt_bbox_dimensions)
```

Prefer translation along world X. Use another axis only when X causes overlap or poor visibility.

## Independence from the GT

The generated geometry and surface assets must not depend on GT objects or GT data.

Do not:

* duplicate GT objects or mesh datablocks;
* import or append the source model;
* copy or serialize the complete GT vertex and face arrays;
* use GT objects as boolean, shrinkwrap, Geometry Nodes, constraint, driver, or instancing inputs;
* construct the reconstruction from evaluated GT meshes;
* duplicate or reuse GT materials, node trees, textures, images, or UV layers;
* extract or bake GT textures into reconstruction assets;
* reference GT materials, textures, images, or objects from generated shader nodes.

Inspection and numerical measurements are allowed.

Small manually defined vertex sets for individual procedural parts are allowed.

Manually observed colors, material properties, texture frequencies, and pattern dimensions may be used to create independent reconstruction materials and textures.

## Safe reruns

Store the complete final script in a Blender Text Editor text block named:

`reconstruct_gt.py`

The script must be safe to rerun.

At the beginning, remove only the previous generated collection:

```python
collection_name = "VLM_RECONSTRUCTION"

existing = bpy.data.collections.get(collection_name)
if existing is not None:
    for obj in list(existing.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(existing)
```

Also remove only reconstruction-owned materials, textures, images, and node groups whose names begin with `recon__` when they are no longer used.

Never clear the full Blender scene or remove objects, materials, textures, images, node groups, or collections outside reconstruction-owned data.

## Materials, textures, and viewport

Preserve all GT materials, textures, images, UV data, and shader settings unchanged.

Create independent reconstruction materials and textures after the geometry has been validated.

Prefer procedural shader nodes and deterministic generated textures where practical. Newly generated image textures are allowed when a procedural shader cannot adequately reproduce an important visible pattern.

Texture and material reconstruction should preserve:

* visible color regions;
* material boundaries;
* approximate roughness;
* approximate metallic response;
* transparency or emission;
* prominent texture scale and direction;
* major bump or normal characteristics.

Do not allow material work to conceal incorrect geometry.

After generation, frame both models in the viewport and keep them visible simultaneously from a useful comparison angle.

Use a viewport shading mode that allows geometry and material comparison. Inspect solid or studio lighting during geometry validation, then inspect material preview or rendered shading during texture validation.

## Iterative geometry reconstruction

After every geometry script execution, compare the GT and reconstruction from multiple viewpoints.

Check:

* total dimensions and aspect ratio;
* part count;
* symmetry and repeated-part spacing;
* part size, position, and orientation;
* attachment and intersections;
* front, rear, side, top, and bottom silhouettes;
* important curvature and distinctive local geometry.

Then:

1. identify the largest geometric discrepancy;
2. modify `reconstruct_gt.py`;
3. rerun the complete script;
4. inspect both models again;
5. repeat until no major visible geometric discrepancy remains.

Prioritize geometry corrections in this order:

1. missing or extra parts;
2. global dimensions and proportions;
3. part position, rotation, and scale;
4. attachment and floating geometry;
5. major silhouettes;
6. distinctive local shapes;
7. minor geometric details.

Do not begin detailed texture reconstruction while major parts, proportions, placements, repeated structures, attachments, or silhouettes are clearly incorrect.

## Iterative texture and material reconstruction

After geometry validation, compare the GT and reconstruction using material preview or rendered shading from multiple viewpoints and lighting angles.

Check:

* base colors;
* color boundaries;
* material assignments;
* roughness and gloss;
* metallic response;
* transparency and emission;
* texture pattern scale;
* texture direction and alignment;
* bump, normal, and displacement appearance;
* consistency across repeated or symmetric components.

Then:

1. identify the largest material or texture discrepancy;
2. modify `reconstruct_gt.py`;
3. rerun the complete script;
4. inspect both models again;
5. repeat until no major visible surface discrepancy remains.

Prioritize surface corrections in this order:

1. missing or incorrect material regions;
2. dominant base colors;
3. roughness, metallic, transparency, and emission;
4. prominent texture patterns;
5. material boundary placement;
6. texture scale and orientation;
7. bump, normal, and minor surface details.

The final reconstruction should be nearly indistinguishable from the GT at a normal viewport comparison distance, except for minor details that are impractical to reproduce procedurally and slight intentional material differences used for comparison.

## Final scene requirements

At completion, the scene must contain:

* the unchanged GT model;
* the `VLM_RECONSTRUCTION` collection;
* the reconstructed objects;
* the independent reconstruction materials and textures;
* the `recon__root` comparison offset;
* the `reconstruct_gt.py` text block.

Both models must be visible side by side.

Before finishing, verify that:

* the GT remains unchanged;
* the geometry reconstruction was completed before detailed texture reconstruction;
* the script runs without errors;
* the script can be safely rerun;
* only generated reconstruction data is removed during reruns;
* no major geometric or proportional discrepancy remains;
* no major visible material or texture discrepancy remains;
* the generated geometry, materials, and textures do not depend on GT datablocks.

## Final response

Report:

* reconstruction collection name;
* Blender text-block name;
* generated object count;
* generated material and texture counts;
* comparison offset and axis;
* main geometry construction techniques;
* main material and texture construction techniques;
* inspected viewpoints;
* number of geometry revision cycles;
* number of texture and material revision cycles;
* main geometry corrections made;
* main texture and material corrections made;
* major remaining geometric differences;
* major remaining material or texture differences.

Do not claim completion unless the script has executed successfully and both the unchanged GT and the reconstruction are visible in the same Blender scene.
