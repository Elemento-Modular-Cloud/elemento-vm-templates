# elemento-vm-templates (UpCloud)

This branch holds **UpCloud server plans** as Elemento VM templates.

Each file under `templates/` is one flavour: `templates/<plan>.json` (for example `templates/2xCPU-4GB.json`).

Queryable catalog: [`index.sqlite`](index.sqlite) — one SQLite file with `templates` + `pci` tables.

Schema reference: [`example.json`](example.json).

## Query examples

```bash
sqlite3 index.sqlite "SELECT name, slots, ramsize FROM templates WHERE family='CLOUD' ORDER BY slots, ramsize;"
sqlite3 index.sqlite "SELECT name, gpu_quantity FROM templates WHERE family='GPU' ORDER BY gpu_quantity DESC;"
sqlite3 index.sqlite "SELECT t.name, p.vendor, p.model, p.quantity FROM templates t JOIN pci p ON p.template_name=t.name;"
```

## Regenerating

Templates are produced by [`scripts/generate_upcloud_templates.py`](scripts/generate_upcloud_templates.py).

```bash
# Prefer official API when UPCLOUD_USERNAME / UPCLOUD_PASSWORD are set
python3 scripts/generate_upcloud_templates.py --source api

# Or public docs fallback (cloud + GPU configuration pages)
python3 scripts/generate_upcloud_templates.py --source docs
```

Mapping: plan `core_number` → `cpu.slots`, `memory_amount` (MiB) → `ram.ramsize`, any accelerator `gpu_amount`/`gpu_model` → `pci`. Plan names like `GPU-8xCPU-64GB-1xL40S` are also parsed when using the docs source.
