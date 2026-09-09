# elemento-vm-templates (Azure)

This branch holds **Azure Virtual Machine sizes** as Elemento VM templates.

Each file under `templates/` is one flavour: `templates/<SKU>.json` (for example `templates/Standard_D2s_v5.json`).

Queryable catalog: [`index.sqlite`](index.sqlite) — one SQLite file with `templates` + `pci` tables.

Schema reference: [`example.json`](example.json).

## Query examples

```bash
sqlite3 index.sqlite "SELECT name, slots, ramsize FROM templates WHERE family='dsv5' ORDER BY slots;"
sqlite3 index.sqlite "SELECT name, gpu_quantity FROM templates WHERE gpu_quantity > 0 ORDER BY gpu_quantity DESC LIMIT 20;"
sqlite3 index.sqlite "SELECT t.name, p.vendor, p.model, p.quantity FROM templates t JOIN pci p ON p.template_name=t.name WHERE t.name LIKE 'Standard_NC%';"
```

## Regenerating

Templates are produced by [`scripts/generate_azure_templates.py`](scripts/generate_azure_templates.py).

```bash
# Public catalog fallback (instances.vantage.sh)
python3 scripts/generate_azure_templates.py --source vantage
```

Mapping: `vcpu`/`vcpus_available` → `cpu.slots`, memory GiB → `ram.ramsize` (MiB), `arch` → `cpu.archs`, any accelerator in `GPU` → `pci` (NVIDIA `10de`, AMD `1002`, etc.).
