# Overview charts

Source of the three product-level charts in `docs/assets/`:

| Chart | Files | Used in |
| --- | --- | --- |
| Local architecture | `architecture-local-{light,dark}.svg` | `README.md` |
| Stage promotion | `stage-promotion-{light,dark}.svg` | `README.md` |
| AWS architecture | `architecture-aws-{light,dark}.svg` | `docs/architecture/aws-eks-poc.md` |

Regenerate all six after editing `charts.py`:

```bash
python3 docs/architecture/diagrams/src/overview/charts.py
```

It needs only the Python standard library. Each SVG is self-contained:
colours are literal per theme, text uses the viewer's system fonts, and every
logo is embedded once from `logos.json` and reused with `<use>`. That is what
GitHub needs, because it renders README images through `<img>`, which runs no
script and loads no web font. The pages embed both variants with `<picture>` and
`prefers-color-scheme`.

## Logos

Every logo in `logos.json` comes from the project's own repository or brand
source. None is redrawn. The `source` field of each entry records where it came
from. The rules for adding one:

- Take it from the project's repository, its CNCF artwork entry, simple-icons
  (in the brand's published colour), or the official AWS Architecture Icons
  (`awslabs/aws-icons-for-plantuml`).
- Crop a wordmark to its mark when the chart needs only the icon, and say so in
  `source`.
- If no official file can be found, label the node with text instead of
  approximating the logo.
