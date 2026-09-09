#!/usr/bin/env python3
"""Generate Elemento VM templates from the full GCP Compute Engine machine type catalog."""

from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

NVIDIA_VENDOR = "10de"
NVIDIA_GPU_MODEL_IDS: dict[str, str] = {
    "nvidia-tesla-t4": "1eb8",
    "nvidia-tesla-v100": "1db4",
    "nvidia-tesla-p100": "15f8",
    "nvidia-tesla-p4": "1bb3",
    "nvidia-tesla-k80": "102d",
    "nvidia-tesla-a100": "20b0",
    "nvidia-a100-80gb": "20b5",
    "nvidia-l4": "27b8",
    "nvidia-l40s": "26b9",
    "nvidia-h100-80gb": "2330",
    "nvidia-h100-mega-80gb": "2330",
    "nvidia-h200-141gb": "2335",
    "nvidia-h200": "2335",
    "nvidia-tesla-a100-80gb": "20b5",
    "nvidia-rtx-pro-6000": "2bb5",
}

# GCP families known to be Arm (Ampere Altra / Google Axion).
ARM_FAMILY_PREFIXES = ("t2a-", "c4a-", "n4a-")

VANTAGE_GCP_URL = "https://instances.vantage.sh/gcp/instances.json"


def resolve_nvidia_model(gpu_name: str) -> str:
    name = gpu_name.strip().lower()
    if name in NVIDIA_GPU_MODEL_IDS:
        return NVIDIA_GPU_MODEL_IDS[name]
    for key, model in sorted(NVIDIA_GPU_MODEL_IDS.items(), key=lambda kv: -len(kv[0])):
        if key in name:
            return model
    return "0000"


def is_nvidia_accelerator(accel_type: str | None) -> bool:
    blob = (accel_type or "").lower()
    if any(tok in blob for tok in ("amd", "radeon", "tpu", "intel-")):
        return False
    return "nvidia" in blob or "tesla" in blob or "l4" in blob or "h100" in blob or "h200" in blob or "a100" in blob


def infer_archs(instance_type: str, architecture: str | None = None) -> list[str]:
    if architecture:
        key = architecture.upper().replace("-", "_")
        if key in ("ARM64", "AARCH64"):
            return ["AARCH64"]
        if key in ("X86_64", "AMD64"):
            return ["X86_64"]
    if instance_type.startswith(ARM_FAMILY_PREFIXES):
        return ["AARCH64"]
    return ["X86_64"]


def cpu_flags(archs: list[str]) -> list[str]:
    if any(a in ("X86_64", "X86") for a in archs):
        return ["sse2", "avx2"]
    return []


def family_of(instance_type: str) -> str:
    # e.g. a2-highgpu-1g → a2 ; n2-standard-4 → n2
    return instance_type.split("-", 1)[0]


def build_description(
    instance_type: str,
    family_label: str,
    vcpus: int,
    ram_mib: int,
    gpu_name: str | None,
    gpu_count: int | None,
) -> str:
    parts = [
        f"GCP Compute Engine {instance_type} ({family_label})",
        f"{vcpus} vCPU",
        f"{ram_mib} MiB RAM",
    ]
    if gpu_name and gpu_count:
        parts.append(f"{gpu_count}x {gpu_name}")
    return "; ".join(parts)


def template_from_machine(mt: dict[str, Any]) -> dict[str, Any]:
    instance_type = mt["name"]
    vcpus = int(mt["guestCpus"])
    ram_mib = int(mt["memoryMb"])
    archs = infer_archs(instance_type, mt.get("architecture"))
    family_label = mt.get("_Family") or family_of(instance_type)

    accelerators = list(mt.get("accelerators") or [])
    gpu_name = None
    gpu_count = 0
    nvidia_pci: list[dict[str, Any]] = []
    for accel in accelerators:
        atype = accel.get("guestAcceleratorType") or accel.get("type") or ""
        count = int(accel.get("guestAcceleratorCount") or accel.get("count") or 0)
        if not count:
            continue
        if is_nvidia_accelerator(atype):
            model = resolve_nvidia_model(atype)
            nvidia_pci.append(
                {"vendor": NVIDIA_VENDOR, "model": model, "quantity": count}
            )
            gpu_name = atype
            gpu_count += count

    tmpl: dict[str, Any] = {
        "info": {
            "name": instance_type,
            "description": build_description(
                instance_type, family_label, vcpus, ram_mib, gpu_name, gpu_count or None
            ),
        },
        "cpu": {
            "slots": vcpus,
            "overprovision": 2,
            "allowSMT": False,
            "archs": archs,
            "flags": cpu_flags(archs),
        },
        "ram": {
            "ramsize": ram_mib,
            "reqECC": False,
        },
    }
    if nvidia_pci:
        # Merge identical vendor/model rows.
        merged: dict[tuple[str, str], int] = {}
        for row in nvidia_pci:
            key = (row["vendor"], row["model"])
            merged[key] = merged.get(key, 0) + int(row["quantity"])
        tmpl["pci"] = [
            {"vendor": v, "model": m, "quantity": q} for (v, m), q in merged.items()
        ]
        if any(p["model"] == "0000" for p in tmpl["pci"]):
            tmpl["info"]["description"] += "; unmapped NVIDIA GPU model (review pci.model)"
    return tmpl


def vantage_to_machine(item: dict[str, Any]) -> dict[str, Any]:
    instance_type = item["instance_type"]
    memory_gb = float(item.get("memory") or 0)
    ram_mib = int(round(memory_gb * 1024))
    accelerators = []
    gpu_count = int(item.get("GPU") or 0)
    gpu_model = item.get("GPU_model")
    if gpu_count and gpu_model:
        accelerators.append(
            {
                "guestAcceleratorType": str(gpu_model),
                "guestAcceleratorCount": gpu_count,
            }
        )
    return {
        "name": instance_type,
        "guestCpus": int(item.get("vCPU") or 0),
        "memoryMb": ram_mib,
        "architecture": None,
        "accelerators": accelerators,
        "_Family": item.get("family") or family_of(instance_type),
    }


def fetch_via_gcloud(project: str, zone: str) -> list[dict[str, Any]]:
    cmd = [
        "gcloud",
        "compute",
        "machine-types",
        "list",
        f"--project={project}",
        f"--zones={zone}",
        "--format=json",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    items = json.loads(proc.stdout)
    # Deduplicate by name (same type can appear if multiple zones requested).
    by_name: dict[str, dict[str, Any]] = {}
    for it in items:
        name = it.get("name")
        if name and name not in by_name:
            by_name[name] = it
    return list(by_name.values())


def fetch_via_vantage() -> list[dict[str, Any]]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        VANTAGE_GCP_URL,
        headers={"User-Agent": "elemento-vm-templates-generator/1.0"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=180) as resp:
        payload = json.load(resp)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected vantage GCP catalog format")
    return [vantage_to_machine(item) for item in payload]


def load_catalog(source: str, project: str, zone: str) -> tuple[list[dict[str, Any]], str]:
    errors: list[str] = []
    if source in ("auto", "gcloud"):
        try:
            return fetch_via_gcloud(project, zone), "gcloud"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gcloud: {exc}")
            if source == "gcloud":
                raise RuntimeError("; ".join(errors)) from exc
    if source in ("auto", "vantage"):
        try:
            return fetch_via_vantage(), "vantage"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"vantage: {exc}")
            raise RuntimeError("; ".join(errors)) from exc
    raise RuntimeError(f"Unknown source {source!r}")


def index_entry(tmpl: dict[str, Any], rel_file: str) -> dict[str, Any]:
    name = tmpl["info"]["name"]
    pci = tmpl.get("pci") or []
    return {
        "name": name,
        "family": family_of(name),
        "file": rel_file,
        "description": tmpl["info"].get("description", ""),
        "slots": int(tmpl["cpu"]["slots"]),
        "ramsize": int(tmpl["ram"]["ramsize"]),
        "archs": list(tmpl["cpu"].get("archs") or []),
        "flags": list(tmpl["cpu"].get("flags") or []),
        "overprovision": int(tmpl["cpu"].get("overprovision", 2)),
        "allow_smt": 1 if tmpl["cpu"].get("allowSMT") else 0,
        "req_ecc": 1 if tmpl["ram"].get("reqECC") else 0,
        "gpu_quantity": sum(int(p.get("quantity") or 0) for p in pci),
        "pci": pci,
    }


def write_sqlite(entries: list[dict[str, Any]], db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE templates (
                name TEXT PRIMARY KEY,
                family TEXT NOT NULL,
                file TEXT NOT NULL,
                description TEXT NOT NULL,
                slots INTEGER NOT NULL,
                ramsize INTEGER NOT NULL,
                archs TEXT NOT NULL,
                flags TEXT NOT NULL,
                overprovision INTEGER NOT NULL,
                allow_smt INTEGER NOT NULL,
                req_ecc INTEGER NOT NULL,
                gpu_quantity INTEGER NOT NULL
            );
            CREATE TABLE pci (
                id INTEGER PRIMARY KEY,
                template_name TEXT NOT NULL REFERENCES templates(name),
                vendor TEXT NOT NULL,
                model TEXT NOT NULL,
                quantity INTEGER NOT NULL
            );
            CREATE INDEX idx_templates_family ON templates(family);
            CREATE INDEX idx_templates_slots ON templates(slots);
            CREATE INDEX idx_templates_ramsize ON templates(ramsize);
            CREATE INDEX idx_templates_gpu ON templates(gpu_quantity);
            CREATE INDEX idx_pci_template ON pci(template_name);
            """
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?), (?, ?)",
            ("provider", "gcp", "count", str(len(entries))),
        )
        conn.executemany(
            """
            INSERT INTO templates(
                name, family, file, description, slots, ramsize, archs, flags,
                overprovision, allow_smt, req_ecc, gpu_quantity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e["name"],
                    e["family"],
                    e["file"],
                    e["description"],
                    e["slots"],
                    e["ramsize"],
                    json.dumps(e["archs"], separators=(",", ":")),
                    json.dumps(e["flags"], separators=(",", ":")),
                    e["overprovision"],
                    e["allow_smt"],
                    e["req_ecc"],
                    e["gpu_quantity"],
                )
                for e in entries
            ],
        )
        pci_rows = [
            (
                e["name"],
                str(p.get("vendor") or ""),
                str(p.get("model") or ""),
                int(p.get("quantity") or 0),
            )
            for e in entries
            for p in (e.get("pci") or [])
        ]
        if pci_rows:
            conn.executemany(
                """
                INSERT INTO pci(template_name, vendor, model, quantity)
                VALUES (?, ?, ?, ?)
                """,
                pci_rows,
            )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def write_templates(
    items: list[dict[str, Any]], out_dir: Path, db_path: Path | None = None
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.json"):
        path.unlink()

    entries: list[dict[str, Any]] = []
    count = 0
    for raw in sorted(items, key=lambda x: x["name"]):
        if not raw.get("name") or not raw.get("guestCpus"):
            continue
        tmpl = template_from_machine(raw)
        name = raw["name"]
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(tmpl, indent=4) + "\n", encoding="utf-8")
        entries.append(index_entry(tmpl, f"templates/{name}.json"))
        count += 1

    if db_path is None:
        db_path = out_dir.parent / "index.sqlite"
    write_sqlite(entries, db_path)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    parser.add_argument("--out-dir", type=Path, default=root / "templates")
    parser.add_argument("--db", type=Path, default=root / "index.sqlite")
    parser.add_argument("--project", default="elemento-public")
    parser.add_argument("--zone", default="us-central1-a")
    parser.add_argument(
        "--catalog-file",
        type=Path,
        default=None,
        help="Optional local vantage/GCP JSON catalog file",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "gcloud", "vantage"),
        default="auto",
        help="Catalog source (auto tries gcloud then vantage fallback)",
    )
    args = parser.parse_args()

    if args.catalog_file:
        raw = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        if isinstance(raw, list) and raw and "instance_type" in raw[0]:
            items = [vantage_to_machine(item) for item in raw]
            used = f"file:{args.catalog_file}"
        elif isinstance(raw, list) and raw and "guestCpus" in raw[0]:
            items = list(raw)
            used = f"file:{args.catalog_file}"
        else:
            raise SystemExit(f"Unrecognized catalog format in {args.catalog_file}")
    else:
        items, used = load_catalog(args.source, args.project, args.zone)

    n = write_templates(items, args.out_dir, db_path=args.db)
    print(
        f"Wrote {n} templates to {args.out_dir} and SQLite catalog {args.db} (source={used})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
