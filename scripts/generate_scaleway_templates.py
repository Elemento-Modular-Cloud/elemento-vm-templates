#!/usr/bin/env python3
"""Generate Elemento VM templates from Scaleway Instance product types."""

from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any

NVIDIA_VENDOR = "10de"
NVIDIA_GPU_MODEL_IDS: dict[str, str] = {
    "p100": "15f8",
    "l4": "27b8",
    "l40s": "26b9",
    "l40": "26b5",
    "h100-pcie": "2331",
    "h100-sxm": "2330",
    "h100": "2330",
    "h200": "2335",
    "b300-sxm": "3182",
    "b300": "3182",
    "b200": "2901",
    "t4": "1eb8",
    "v100": "1db4",
    "a100": "20b0",
}

DEFAULT_ZONES = [
    "fr-par-1",
    "fr-par-2",
    "fr-par-3",
    "nl-ams-1",
    "nl-ams-2",
    "nl-ams-3",
    "pl-waw-1",
    "pl-waw-2",
    "pl-waw-3",
]


def family_of(name: str) -> str:
    return name.split("-", 1)[0]


def map_arch(arch: str | None) -> list[str]:
    key = (arch or "").lower().replace("-", "_")
    if key in ("arm64", "aarch64"):
        return ["AARCH64"]
    if key in ("x86_64", "amd64"):
        return ["X86_64"]
    if key in ("i386", "x86"):
        return ["X86"]
    return ["X86_64"] if not key else [key.upper()]


def cpu_flags(archs: list[str]) -> list[str]:
    if any(a in ("X86_64", "X86") for a in archs):
        return ["sse2", "avx2"]
    return []


def resolve_nvidia_model(gpu_name: str) -> str:
    name = gpu_name.strip().lower()
    if name in NVIDIA_GPU_MODEL_IDS:
        return NVIDIA_GPU_MODEL_IDS[name]
    for key, model in sorted(NVIDIA_GPU_MODEL_IDS.items(), key=lambda kv: -len(kv[0])):
        if key in name:
            return model
    return "0000"


def bytes_to_mib(n: int) -> int:
    return int(n) // (1024 * 1024)


def template_from_product(name: str, info: dict[str, Any]) -> dict[str, Any]:
    vcpus = int(info.get("ncpus") or 0)
    ram_mib = bytes_to_mib(int(info.get("ram") or 0))
    archs = map_arch(info.get("arch"))
    gpu_count = int(info.get("gpu") or 0)
    gpu_info = info.get("gpu_info") or {}
    gpu_name = gpu_info.get("gpu_name") or gpu_info.get("name")
    manufacturer = (gpu_info.get("gpu_manufacturer") or gpu_info.get("manufacturer") or "").lower()

    parts = [
        f"Scaleway Instance {name} ({family_of(name)})",
        f"{vcpus} vCPU",
        f"{ram_mib} MiB RAM",
    ]
    if gpu_count and gpu_name:
        parts.append(f"{gpu_count}x {gpu_name}")
    description = "; ".join(parts)

    tmpl: dict[str, Any] = {
        "info": {"name": name, "description": description},
        "cpu": {
            "slots": vcpus,
            "overprovision": 2,
            "allowSMT": False,
            "archs": archs,
            "flags": cpu_flags(archs),
        },
        "ram": {"ramsize": ram_mib, "reqECC": False},
    }

    if gpu_count and gpu_name and ("nvidia" in manufacturer or manufacturer == ""):
        # Scaleway GPU instances are NVIDIA; treat empty manufacturer + known name as NVIDIA.
        model = resolve_nvidia_model(str(gpu_name))
        if model == "0000":
            tmpl["info"]["description"] += "; unmapped NVIDIA GPU model (review pci.model)"
        tmpl["pci"] = [
            {"vendor": NVIDIA_VENDOR, "model": model, "quantity": gpu_count}
        ]
    return tmpl


def fetch_zone_products(zone: str) -> dict[str, dict[str, Any]]:
    url = f"https://api.scaleway.com/instance/v1/zones/{zone}/products/servers"
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "elemento-vm-templates/1.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        payload = json.load(resp)
    servers = payload.get("servers") or {}
    if not isinstance(servers, dict):
        raise RuntimeError(f"Unexpected products response for {zone}")
    return servers


def load_catalog(zones: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    merged: dict[str, dict[str, Any]] = {}
    used_zones: list[str] = []
    errors: list[str] = []
    for zone in zones:
        try:
            products = fetch_zone_products(zone)
            used_zones.append(f"{zone}:{len(products)}")
            for name, info in products.items():
                merged.setdefault(name, info)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{zone}: {exc}")
    if not merged:
        raise RuntimeError("; ".join(errors) or "no Scaleway products found")
    return merged, "scaleway-products(" + ",".join(used_zones) + ")"


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
            ("provider", "scaleway", "count", str(len(entries))),
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
    products: dict[str, dict[str, Any]], out_dir: Path, db_path: Path
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.json"):
        path.unlink()

    entries: list[dict[str, Any]] = []
    count = 0
    for name in sorted(products):
        info = products[name]
        if not int(info.get("ncpus") or 0):
            continue
        tmpl = template_from_product(name, info)
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(tmpl, indent=4) + "\n", encoding="utf-8")
        entries.append(index_entry(tmpl, f"templates/{name}.json"))
        count += 1

    write_sqlite(entries, db_path)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    parser.add_argument("--out-dir", type=Path, default=root / "templates")
    parser.add_argument("--db", type=Path, default=root / "index.sqlite")
    parser.add_argument(
        "--zones",
        default=",".join(DEFAULT_ZONES),
        help="Comma-separated Scaleway zones to merge product catalogs from",
    )
    parser.add_argument(
        "--catalog-file",
        type=Path,
        default=None,
        help="Optional local products JSON ({'servers': {...}} or flat name→info map)",
    )
    args = parser.parse_args()

    if args.catalog_file:
        raw = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "servers" in raw:
            products = dict(raw["servers"])
        elif isinstance(raw, dict):
            products = raw
        else:
            raise SystemExit(f"Unrecognized catalog format in {args.catalog_file}")
        used = f"file:{args.catalog_file}"
    else:
        zones = [z.strip() for z in args.zones.split(",") if z.strip()]
        products, used = load_catalog(zones)

    n = write_templates(products, args.out_dir, args.db)
    print(
        f"Wrote {n} templates to {args.out_dir} and SQLite catalog {args.db} (source={used})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
