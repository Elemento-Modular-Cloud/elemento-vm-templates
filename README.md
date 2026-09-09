# elemento-vm-templates (OVHcloud)

This branch holds **OVHcloud Public Cloud instance flavors** as Elemento VM templates.

Each file under `templates/` is one flavour: `templates/<flavor>.json` (for example `templates/b3-8.json`).

Queryable catalog: [`index.sqlite`](index.sqlite) — one SQLite file with `templates` + `pci` tables.

Schema reference: [`example.json`](example.json).

## Query examples

```bash
sqlite3 index.sqlite "SELECT name, slots, ramsize FROM templates WHERE family='b3' ORDER BY slots;"
sqlite3 index.sqlite "SELECT name, gpu_quantity FROM templates WHERE gpu_quantity > 0 ORDER BY gpu_quantity DESC;"
sqlite3 index.sqlite "SELECT t.name, p.vendor, p.model, p.quantity FROM templates t JOIN pci p ON p.template_name=t.name WHERE t.family LIKE 't%';"
```

## Regenerating

Templates are produced by [`scripts/generate_ovh_templates.py`](scripts/generate_ovh_templates.py).

```bash
# Public order catalog (no auth required)
python3 scripts/generate_ovh_templates.py

# Include Windows-licensed win-* variants as well
python3 scripts/generate_ovh_templates.py --include-windows
```

Mapping: technical `cpu.cores` → `cpu.slots`, `memory.size` (GiB) → `ram.ramsize` (MiB), NVIDIA `gpu` → `pci`. Windows `win-*` SKUs are skipped by default (same hardware as Linux flavors).
