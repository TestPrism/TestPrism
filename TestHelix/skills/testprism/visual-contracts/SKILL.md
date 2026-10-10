---
name: visual-contracts
description: "Turn visual requirements into reproducible structural and rendered evidence."
metadata:
  components: [generation, review]
---

Inspect the public reference image and the existing rendering entry point before inventing expected geometry. Choose observable requirements such as text presence, bounding boxes, containment, ordering, overlap or interaction state, according to the public contract.
Use a fixed viewport, fonts, locale and ready-state condition. Check that assets loaded and the intended component actually rendered. Geometry or accessibility assertions may be more stable than whole-image pixel equality; use image differences only with justified tolerance for rendering variation.
Keep generated artifacts and fixtures under agent-tests/. Avoid a single platform-specific screenshot hash, hard-coded implementation selectors or downloading new browser dependencies. When the available environment cannot render the requirement, report the coverage gap instead of substituting an unrelated passing check.
