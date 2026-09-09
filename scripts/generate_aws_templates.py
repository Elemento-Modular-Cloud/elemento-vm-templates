#!/usr/bin/env python3
"""Generate Elemento VM templates from the full AWS EC2 instance type catalog."""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

# NVIDIA PCI vendor + device IDs for GPUs commonly offered on EC2.
NVIDIA_VENDOR = "10de"
NVIDIA_GPU_MODEL_IDS: dict[str, str] = {
    "t4": "1eb8",
    "t4g": "1eb4",
    "a10g": "2237",
    "a10": "2236",
    "l4": "27b8",
    "l40s": "26b9",
    "l40": "26b5",
    "v100": "1db4",
    "a100 80gb": "20b5",
    "a100-80gb": "20b5",
    "a100 40gb": "20b0",
    "a100-40gb": "20b0",
    "a100": "20b0",
    "h100 nvl": "2321",
    "h100 pcie": "2331",
    "h100": "2330",
    "h200": "2335",
    "p4d": "20b0",  # A100 SXM
    "p4de": "20b2",  # A100 80GB SXM
    "p3": "1db4",
    "p2": "15f8",  # Tesla P100
    "g3": "1db1",  # Tesla M60 often reported differently; keep as V100-era fallback unused
    "m60": "13f2",
    "k80": "102d",
    "k520": "118a",
    "grid k520": "118a",
    "a10g tensor core": "2237",
    "tesla t4": "1eb8",
    "tesla v100": "1db4",
    "tesla p100": "15f8",
    "tesla p4": "1bb3",
    "tesla k80": "102d",
    "tesla m60": "13f2",
    "nvidia a10g": "2237",
    "nvidia l4": "27b8",
    "nvidia l40s": "26b9",
    "nvidia h100": "2330",
    "nvidia h200": "2335",
    "nvidia a100": "20b0",
    "b200": "2901",
    "b300": "3182",
    "rtx pro 6000": "2bb5",
}

VANTAGE_INSTANCES_URL = "https://instances.vantage.sh/instances.json"


def map_archs(arches: list[str]) -> list[str]:
    mapped: list[str] = []
    for arch in arches:
        key = arch.lower().replace("-", "_")
        if key in ("x86_64", "x86-64", "amd64"):
            mapped.append("X86_64")
        elif key in ("arm64", "aarch64"):
            mapped.append("AARCH64")
        elif key in ("i386", "x86"):
            mapped.append("X86")
        else:
            mapped.append(arch.upper())
    # Prefer 64-bit; drop X86 when X86_64 is also present.
    if "X86_64" in mapped and "X86" in mapped:
        mapped = [a for a in mapped if a != "X86"]
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for a in mapped:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def cpu_flags(archs: list[str]) -> list[str]:
    if any(a in ("X86_64", "X86") for a in archs):
        return ["sse2", "avx2"]
    return []


def resolve_nvidia_model(gpu_name: str) -> str:
    name = gpu_name.strip().lower()
    if name in NVIDIA_GPU_MODEL_IDS:
        return NVIDIA_GPU_MODEL_IDS[name]
    # Prefer longer/more specific keys first.
    for key, model in sorted(NVIDIA_GPU_MODEL_IDS.items(), key=lambda kv: -len(kv[0])):
        if key in name:
            return model
    return "0000"


def is_nvidia_gpu(manufacturer: str | None, name: str | None) -> bool:
    blob = f"{manufacturer or ''} {name or ''}".lower()
    # Explicit non-NVIDIA accelerators must never get NVIDIA pci entries.
    non_nvidia = (
        "amd",
        "radeon",
        "qualcomm",
        "habana",
        "gaudi",
        "inferentia",
        "trainium",
        "xilinx",
        "fpga",
    )
    if any(tok in blob for tok in non_nvidia):
        return False
    return "nvidia" in blob or "tesla" in blob


def build_description(
    instance_type: str,
    vcpus: int,
    ram_mib: int,
    usage_classes: list[str],
    gpu_name: str | None,
    gpu_count: int | None,
    family_label: str | None = None,
) -> str:
    type_family = instance_type.split(".", 1)[0]
    classes = ", ".join(usage_classes) if usage_classes else "general"
    label = family_label or type_family
    parts = [
        f"AWS EC2 {instance_type} ({label})",
        f"{vcpus} vCPU",
        f"{ram_mib} MiB RAM",
        f"usage: {classes}",
    ]
    if gpu_name and gpu_count:
        parts.append(f"{gpu_count}x {gpu_name}")
    return "; ".join(parts)


def template_from_aws(it: dict[str, Any]) -> dict[str, Any]:
    instance_type = it["InstanceType"]
    vcpus = int(it["VCpuInfo"]["DefaultVCpus"])
    ram_mib = int(it["MemoryInfo"]["SizeInMiB"])
    archs = map_archs(list(it.get("ProcessorInfo", {}).get("SupportedArchitectures", [])))
    usage = list(it.get("SupportedUsageClasses", []))

    gpu_info = it.get("GpuInfo") or {}
    gpu_devices = list(gpu_info.get("Gpus") or [])
    gpu_name = None
    gpu_count = None
    manufacturer = None
    if gpu_devices:
        # Aggregate identical NVIDIA devices; AWS usually lists one entry with Count.
        first = gpu_devices[0]
        manufacturer = first.get("Manufacturer")
        gpu_name = first.get("Name")
        gpu_count = int(first.get("Count") or 0)
        for extra in gpu_devices[1:]:
            if (extra.get("Name") or "") == (gpu_name or ""):
                gpu_count += int(extra.get("Count") or 0)

    family_label = it.get("_Family")
    tmpl: dict[str, Any] = {
        "info": {
            "name": instance_type,
            "description": build_description(
                instance_type,
                vcpus,
                ram_mib,
                usage,
                gpu_name,
                gpu_count,
                family_label=family_label if isinstance(family_label, str) else None,
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

    if gpu_name and gpu_count and is_nvidia_gpu(manufacturer, gpu_name):
        model = resolve_nvidia_model(gpu_name)
        desc = tmpl["info"]["description"]
        if model == "0000":
            desc = f"{desc}; unmapped NVIDIA GPU model (review pci.model)"
            tmpl["info"]["description"] = desc
        tmpl["pci"] = [
            {
                "vendor": NVIDIA_VENDOR,
                "model": model,
                "quantity": gpu_count,
            }
        ]

    return tmpl


def aws_to_normalized(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize AWS DescribeInstanceTypes item (already AWS-shaped)."""
    return raw


def vantage_to_aws_shape(item: dict[str, Any]) -> dict[str, Any]:
    """Convert instances.vantage.sh record to DescribeInstanceTypes-like shape."""
    instance_type = item["instance_type"]
    arch = item.get("arch") or ["x86_64"]
    if isinstance(arch, str):
        arch = [arch]

    gpus = []
    gpu_name = item.get("GPU_model") or item.get("gpu_model")
    gpu_count = item.get("GPU") or item.get("gpu") or 0
    try:
        gpu_count = int(float(gpu_count))
    except (TypeError, ValueError):
        gpu_count = 0
    manufacturer = item.get("GPU_manufacturer") or item.get("gpu_manufacturer")
    if gpu_count and gpu_name:
        gname = str(gpu_name)
        if not manufacturer:
            lower = gname.lower()
            if "nvidia" in lower or "tesla" in lower:
                manufacturer = "NVIDIA"
            elif "amd" in lower or "radeon" in lower:
                manufacturer = "AMD"
            elif "qualcomm" in lower:
                manufacturer = "Qualcomm"
        gpus.append(
            {
                "Name": gname,
                "Manufacturer": manufacturer or "Unknown",
                "Count": gpu_count,
            }
        )

    usage = []
    if item.get("OKA"):
        pass
    # vantage uses various capability flags; default on-demand
    usage = ["on-demand"]
    if item.get("spot_price") is not None or item.get("pricing"):
        usage.append("spot")

    memory = item.get("memory")
    # vantage memory is often GiB float
    if memory is None:
        ram_mib = 0
    else:
        ram_mib = int(round(float(memory) * 1024))

    vcpus = int(item.get("vCPU") or item.get("vcpu") or 0)

    return {
        "InstanceType": instance_type,
        "VCpuInfo": {"DefaultVCpus": vcpus},
        "MemoryInfo": {"SizeInMiB": ram_mib},
        "ProcessorInfo": {"SupportedArchitectures": arch},
        "SupportedUsageClasses": usage,
        "GpuInfo": {"Gpus": gpus} if gpus else {},
        "_Family": item.get("family") or item.get("pretty_name") or instance_type.split(".", 1)[0],
    }


def fetch_via_aws_cli(region: str) -> list[dict[str, Any]]:
    cmd = [
        "aws",
        "ec2",
        "describe-instance-types",
        "--include-unsupported-in-region",
        "--region",
        region,
        "--output",
        "json",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)
    return list(data.get("InstanceTypes") or [])


def fetch_via_boto3(region: str) -> list[dict[str, Any]]:
    import boto3  # type: ignore

    client = boto3.client("ec2", region_name=region)
    paginator = client.get_paginator("describe_instance_types")
    items: list[dict[str, Any]] = []
    for page in paginator.paginate(IncludeUnsupportedInRegion=True):
        items.extend(page.get("InstanceTypes") or [])
    return items


def fetch_via_vantage() -> list[dict[str, Any]]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        VANTAGE_INSTANCES_URL,
        headers={"User-Agent": "elemento-vm-templates-generator/1.0"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
        payload = json.load(resp)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected vantage catalog format")
    return [vantage_to_aws_shape(item) for item in payload]


def load_catalog(region: str, source: str) -> tuple[list[dict[str, Any]], str]:
    errors: list[str] = []
    if source in ("auto", "aws"):
        try:
            return fetch_via_aws_cli(region), "aws-cli"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"aws-cli: {exc}")
        try:
            return fetch_via_boto3(region), "boto3"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"boto3: {exc}")
        if source == "aws":
            raise RuntimeError("; ".join(errors))
    if source in ("auto", "vantage"):
        try:
            return fetch_via_vantage(), "vantage"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"vantage: {exc}")
            raise RuntimeError("; ".join(errors)) from exc
    raise RuntimeError(f"Unknown source {source!r}")


def index_entry(tmpl: dict[str, Any], rel_file: str) -> dict[str, Any]:
    name = tmpl["info"]["name"]
    family = name.split(".", 1)[0]
    pci = tmpl.get("pci") or []
    return {
        "name": name,
        "family": family,
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
    import sqlite3

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
            ("provider", "aws", "count", str(len(entries))),
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
        pci_rows: list[tuple[str, str, str, int]] = []
        for e in entries:
            for p in e.get("pci") or []:
                pci_rows.append(
                    (
                        e["name"],
                        str(p.get("vendor") or ""),
                        str(p.get("model") or ""),
                        int(p.get("quantity") or 0),
                    )
                )
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
    # Remove previously generated EC2 templates (keep directory).
    for path in out_dir.glob("*.json"):
        path.unlink()

    count = 0
    entries: list[dict[str, Any]] = []
    for raw in sorted(items, key=lambda x: x["InstanceType"]):
        it = aws_to_normalized(raw)
        if not it.get("InstanceType"):
            continue
        if not it.get("VCpuInfo", {}).get("DefaultVCpus"):
            continue
        tmpl = template_from_aws(it)
        instance_type = it["InstanceType"]
        path = out_dir / f"{instance_type}.json"
        path.write_text(json.dumps(tmpl, indent=4) + "\n", encoding="utf-8")
        rel = f"templates/{instance_type}.json"
        entries.append(index_entry(tmpl, rel))
        count += 1

    if db_path is None:
        db_path = out_dir.parent / "index.sqlite"
    write_sqlite(entries, db_path)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=root / "templates",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=root / "index.sqlite",
        help="Path for the SQLite catalog",
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--catalog-file",
        type=Path,
        default=None,
        help="Optional local vantage/AWS JSON catalog file",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "aws", "vantage"),
        default="auto",
        help="Catalog source (auto tries AWS then vantage fallback)",
    )
    args = parser.parse_args()

    if args.catalog_file:
        raw = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        if isinstance(raw, list) and raw and "instance_type" in raw[0]:
            items = [vantage_to_aws_shape(item) for item in raw]
            used = f"file:{args.catalog_file}"
        elif isinstance(raw, dict) and "InstanceTypes" in raw:
            items = list(raw["InstanceTypes"])
            used = f"file:{args.catalog_file}"
        else:
            raise SystemExit(f"Unrecognized catalog format in {args.catalog_file}")
    else:
        items, used = load_catalog(args.region, args.source)
    n = write_templates(items, args.out_dir, db_path=args.db)
    print(
        f"Wrote {n} templates to {args.out_dir} and SQLite catalog {args.db} (source={used})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
