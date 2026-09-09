# elemento-vm-templates (GCP)

This branch holds **Google Cloud Compute Engine machine types** as Elemento VM templates.

Each file under `templates/` is one flavour: `templates/<machine-type>.json` (for example `templates/n2-standard-4.json`).

Queryable catalog: [`index.sqlite`](index.sqlite) — one SQLite file with `templates` + `pci` tables.

Schema reference: [`example.json`](example.json).

## Query examples

```bash
sqlite3 index.sqlite "SELECT name, slots, ramsize FROM templates WHERE family='e2' ORDER BY slots;"
sqlite3 index.sqlite "SELECT name, gpu_quantity FROM templates WHERE gpu_quantity > 0 ORDER BY gpu_quantity DESC;"
sqlite3 index.sqlite "SELECT t.name, p.vendor, p.model, p.quantity FROM templates t JOIN pci p ON p.template_name=t.name WHERE t.family='a2';"
```

## Regenerating

Templates are produced by [`scripts/generate_gcp_templates.py`](scripts/generate_gcp_templates.py).

```bash
# Prefer gcloud when authenticated
python3 scripts/generate_gcp_templates.py --source gcloud --project YOUR_PROJECT --zone us-central1-a

# Or public catalog fallback (instances.vantage.sh)
python3 scripts/generate_gcp_templates.py --source vantage
```

Mapping: advertised vCPUs → `cpu.slots`, memory MiB → `ram.ramsize`, architectures → `cpu.archs` (Arm families `t2a`/`c4a`/`n4a` → `AARCH64`), any accelerator → `pci`.
