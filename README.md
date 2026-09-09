# elemento-vm-templates (AWS)

This branch holds **AWS EC2 instance types** as Elemento VM templates.

Each file under `templates/` is one flavour: `templates/<instance-type>.json` (for example `templates/t3.medium.json`).

Queryable catalog: [`index.sqlite`](index.sqlite) — one SQLite file with `templates` + `pci` tables.

Schema reference: [`example.json`](example.json).

## Query examples

```bash
sqlite3 index.sqlite "SELECT name, slots, ramsize FROM templates WHERE family='t4g' ORDER BY slots;"
sqlite3 index.sqlite "SELECT name, gpu_quantity FROM templates WHERE gpu_quantity > 0 ORDER BY gpu_quantity DESC LIMIT 10;"
sqlite3 index.sqlite "SELECT t.name, p.vendor, p.model, p.quantity FROM templates t JOIN pci p ON p.template_name=t.name WHERE t.name='g4dn.xlarge';"
```

## Regenerating

Templates are produced by [`scripts/generate_aws_templates.py`](scripts/generate_aws_templates.py).

```bash
# Prefer official AWS API when credentials are available
python3 scripts/generate_aws_templates.py --source aws --region us-east-1

# Or public catalog fallback (instances.vantage.sh)
python3 scripts/generate_aws_templates.py --source vantage
```

Mapping: advertised vCPUs → `cpu.slots`, memory MiB → `ram.ramsize`, architectures → `cpu.archs`, NVIDIA GPUs → `pci` (`vendor` `10de` + device model ID).
