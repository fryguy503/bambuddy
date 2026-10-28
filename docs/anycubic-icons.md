# Anycubic printer icons

The dashboard uses bundled 512 x 512 RGBA PNG cutouts in
`frontend/public/img/printers/`. Icons are displayed for the configured model
through the shared `getPrinterImage` resolver, including connected printer cards
and printer information views. No Anycubic image service is used at runtime.

The built-in `image_gen` tool produced these cutouts. Its transparent output was
downscaled for the dashboard; the original photographs are not bundled. Existing
manufacturer/model marks identify the depicted hardware and belong to Anycubic.

| Icon filename | Reference |
| --- | --- |
| anycubic-kobra-s1.png | User-supplied Kobra S1 photograph, with the surrounding Refurbished badge removed |
| anycubic-kobra-s1-max.png | [Official S1 Max product photograph](https://store.anycubic.com/products/kobra-s1-max) |
| anycubic-kobra-3.png | [Official Kobra 3 Combo product photograph](https://store.anycubic.com/products/kobra-3-combo) |
| anycubic-kobra-3-v2.png | [Official Kobra 3 V2 product photograph](https://store.anycubic.com/products/kobra-3-v2) |
| anycubic-kobra-3-max.png | [Official Kobra 3 Max product photograph](https://store.anycubic.com/products/kobra-3-max) |
| anycubic-kobra-4.png | [Official Kobra 4 product photograph](https://store.anycubic.com/products/kobra-4-3d-printer) |
| anycubic-kobra-x.png | [Official Kobra X product photograph](https://store.anycubic.com/products/kobra-x) |

## S1 edit prompt

```text
Edit target: the attached Anycubic Kobra S1 product photograph. Intended use: a small, accurate printer model icon in the Bambuddy printer dashboard, consistent with photographic product cutouts. Extract ONLY the complete printer including its top touchscreen and upper small cylindrical fitting, preserving the exact frontal view, proportions, silver enclosure, dark glass door, white door handle, internal print head and original existing branding. Remove the white background, all surrounding empty space, and the separate Refurbished badge at top right. Do not include an ACE unit, extra printer, pedestal, background, new text, border, invented design details or graphical labels. Center the full isolated printer on a square genuinely transparent canvas, with narrow balanced padding and no clipped touchscreen or feet. Use a crisp product-photo cutout suitable for display on both light and dark UI backgrounds. Preserve the actual printer design from the edit target; do not redesign or stylize it.
```

## Other model prompts

Each model used this template with its own reference photo, `{model}` and
`{model-specific instruction}` below; `transparent_background` was true:

```text
Edit target: the attached official Anycubic {model} product photo. Create an accurate photographic cutout for the Bambuddy printer dashboard model icon. Preserve the exact actual printer design, proportions, color, viewing angle, hardware, feet and attached touchscreen. {model-specific instruction} Remove the white background and surrounding empty space; make the open spaces between frame members genuinely transparent as well. Center the complete isolated printer on a square transparent canvas with narrow balanced padding and nothing clipped. Keep fine cables recognizable. No separate ACE boxes, added objects, sample prints, background, pedestal, border, overlay text or new labels. Do not redesign or stylize the printer. Preserve original existing manufacturer/model branding on its actual hardware.
```

| Model | Model-specific instruction |
| --- | --- |
| Kobra 3 | Remove the separate ACE Pro box on the left and the external tubing tails that lead toward that removed box. Retain the printer's structural parts and its attached screen. |
| Kobra 3 Max | Retain the tall frame, wide bed, original screen and attached orange cable. |
| Kobra 3 V2 | Retain its exact head, wide base, camera, screen and original attached tubing. |
| Kobra 4 | Remove the blue watering-can sample print, leaving the existing build plate empty. |
| Kobra S1 Max | Remove the blue/red spaceship sample print inside the enclosure, leaving an empty build plate. Preserve the Max's distinctive top-front panel, screen location and enclosure. |
| Kobra X | Remove the purple dragon sample print, leaving an empty bed. Retain its four top-mounted spools, spool holders and connected tubes because these are distinctive to this model. |
