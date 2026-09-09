# elemento-vm-templates (Scaleway)

This branch holds **Scaleway Instance types** as Elemento VM templates.

Each file under `templates/` is one flavour: `templates/<TYPE>.json` (for example `templates/PRO2-M.json`).

Queryable catalog: [`index.sqlite`](index.sqlite) — one SQLite file with `templates` + `pci` tables.

Schema reference: [`example.json`](example.json).

## Query examples

```bash
sqlite3 index.sqlite "SELECT name, slots, ramsize FROM templates WHERE family='PRO2' ORDER BY slots;"
sqlite3 index.sqlite "SELECT name, gpu_quantity FROM templates WHERE gpu_quantity > 0 ORDER BY gpu_quantity DESC;"
sqlite3 index.sqlite "SELECT t.name, p.vendor, p.model, p.quantity FROM templates t JOIN pci p ON p.template_name=t.name;"
```

## Regenerating

Templates are produced by [`scripts/generate_scaleway_templates.py`](scripts/generate_scaleway_templates.py).

```bash
# Merge public product catalogs across Scaleway zones (no auth required)
python3 scripts/generate_scaleway_templates.py

# Or restrict zones
python3 scripts/generate_scaleway_templates.py --zones fr-par-1,nl-ams-1,pl-waw-1
```

Mapping: `ncpus` → `cpu.slots`, RAM bytes → `ram.ramsize` (MiB), `arch` → `cpu.archs`, NVIDIA `gpu`/`gpu_info` → `pci`.
