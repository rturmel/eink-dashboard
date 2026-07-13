# assets/

Drop static images here (logos, icons, etc.) that rarely or never change.
Reference them from any `image` widget with:

```json
{"asset": "logo.png"}
```

instead of pushing the image's bytes through the broker on every update.
Any format Pillow can read works (PNG, JPG/JPEG, GIF, BMP, ...).

This directory needs to exist on whichever machine actually renders the
frame -- i.e. copy your logo into `pi_client`'s copy of
`shared/dashboard_render/assets/` (and `preview`'s, if you want it to show
up in the local preview too). Since both point at the same `shared/`
directory in this repo, dropping a file in here once covers both.

PNGs with transparency are handled correctly -- transparent areas are
flattened onto white before the image is quantized down to the panel's
4-color palette, so a logo with a transparent background won't show up
with a black box behind it.
