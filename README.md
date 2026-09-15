# bp2mermaid

Unreal Editor Python tool that turns **Blueprint graphs** into **[Mermaid](https://mermaid.js.org/) markdown**.

Blueprints live in binary `.uasset` files. This script walks graphs and class data in the editor and writes compact markdown you can paste into an LLM or commit to git.

| ![](/assets/bp2mermaid-1.png) | ![](/assets/bp2mermaid-0.png) |
| ----------------------------- | ----------------------------- |

Each Blueprint export includes Class Settings, Class Defaults, `# Components` (CDO property values; engine Scene / Movement / Timeline types are skipped), and every meaningful graph (Event Graph first). Empty stubs and pose-only graphs are skipped.

Also exported from Content:

- `GA_` / `GE_` Gameplay Ability and Gameplay Effect blueprints — class-only markdown when graphs are empty
- `DA_` DataAssets — JSON inside a markdown code fence

## Use

Open a Blueprint. On the Blueprint Editor toolbar, **Export Graph**:

- **Export this Blueprint** — one markdown file, also copied to the clipboard (Windows)
- **Export the project** — every `/Game` Blueprint plus `GA_` / `GE_` / `DA_` assets (skips unchanged)
- **Enable auto-export** — re-export on save

Output: `{Project}/Source/BPMermaids/`, mirroring Content paths.

`/Game/Characters/BP_Hero` → `Source/BPMermaids/Characters/BP_Hero.md`

## Setup

Requires Unreal Editor 5.x with **Python Editor Script Plugin** and **Editor Scripting Utilities**.

1. Copy `Content/Python/editor/bp2mermaid.py` into `{Project}/Content/Python/editor/`.
2. On editor startup (Unreal auto-runs `Content/Python/init_unreal.py`):

```python
import os, sys, unreal
import importlib

editor = os.path.normpath(unreal.Paths.project_content_dir() + "Python/editor")
if editor not in sys.path:
    sys.path.insert(0, editor)

module = importlib.import_module("bp2mermaid")
module.register_menu()
```

Or just run `py bp2mermaid.py` from the Output Log.
