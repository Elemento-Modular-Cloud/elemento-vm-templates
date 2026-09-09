#!/usr/bin/env python3
"""Generate Elemento VM templates from OVHcloud Public Cloud instance flavors."""

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
AMD_VENDOR = "1002"
UNKNOWN_VENDOR = "0000"

ACCELERATOR_MODEL_IDS: dict[str, tuple[str, str]] = {
    "tesla v100s": (NVIDIA_VENDOR, "1df6"),
    "tesla v100": (NVIDIA_VENDOR, "1db4"),
    "v100s": (NVIDIA_VENDOR, "1df6"),
    "v100": (NVIDIA_VENDOR, "1db4"),
    "l4": (NVIDIA_VENDOR, "27b8"),
    "l40s": (NVIDIA_VENDOR, "26b9"),
    "l40": (NVIDIA_VENDOR, "26b5"),
    "h100": (NVIDIA_VENDOR, "2330"),
    "h200": (NVIDIA_VENDOR, "2335"),
    "a100": (NVIDIA_VENDOR, "20b0"),
    "a10": (NVIDIA_VENDOR, "2236"),
    "quadro-rtx5000": (NVIDIA_VENDOR, "1e30"),
    "quadro rtx 5000": (NVIDIA_VENDOR, "1e30"),
    "rtx5000": (NVIDIA_VENDOR, "1e30"),
    "rtx 5000": (NVIDIA_VENDOR, "1e30"),
    "geforce gtx 1060": (NVIDIA_VENDOR, "1c03"),
    "gtx 1060": (NVIDIA_VENDOR, "1c03"),
    "geforce gtx 1070": (NVIDIA_VENDOR, "1b81"),
    "gtx 1070": (NVIDIA_VENDOR, "1b81"),
    "geforce gtx 1080 ti": (NVIDIA_VENDOR, "1b06"),
    "gtx 1080 ti": (NVIDIA_VENDOR, "1b06"),
    "t4": (NVIDIA_VENDOR, "1eb8"),
    "mi25": (AMD_VENDOR, "740c"),
    "radeon": (AMD_VENDOR, "0000"),
}

DEFAULT_CATALOG_URL = (
    "https://eu.api.ovh.com/1.0/order/catalog/public/cloud?ovhSubsidiary=FR"
)


def family_of(name: str) -> str:
    if name.startswith("metal."):
        return name.split("-", 1)[0]
    return name.split("-", 1)[0]


def resolve_accelerator(gpu_name: str) -> tuple[str, str]:
    name = gpu_name.strip().lower()
    for key, (vendor, model) in sorted(
        ACCELERATOR_MODEL_IDS.items(), key=lambda kv: -len(kv[0])
    ):
        if key in name:
            return vendor, model
    if "amd" in name or "radeon" in name:
        return AMD_VENDOR, "0000"
    if name:
        return NVIDIA_VENDOR, "0000"
    return UNKNOWN_VENDOR, "0000"


def cpu_flags(archs: list[str]) -> list[str]:
    if any(a in ("X86_64", "X86") for a in archs):
        return ["sse2", "avx2"]
    return []


def infer_archs(tech: dict[str, Any]) -> list[str]:
    cpu = tech.get("cpu") or {}
    blob = json.dumps(cpu).lower()
    if "arm" in blob or "aarch" in blob or "graviton" in blob:
        return ["AARCH64"]
    return ["X86_64"]


def is_windows_flavor(name: str, tech: dict[str, Any]) -> bool:
    if name.startswith("win-"):
        return True
    return (tech.get("os") or {}).get("family") == "windows"


def flavor_score(plan_code: str, name: str, tech: dict[str, Any]) -> int:
    score = 0
    if tech.get("cpu"):
        score += 5
    if "hour" in plan_code or "consumption" in plan_code:
        score += 2
    if is_windows_flavor(name, tech):
        score -= 20
    return score


def extract_flavors(catalog: dict[str, Any], include_windows: bool) -> list[dict[str, Any]]:
    by_name: dict[str, tuple[int, dict[str, Any]]] = {}
    for addon in catalog.get("addons") or []:
        if addon.get("product") != "publiccloud-instance":
            continue
        tech = ((addon.get("blobs") or {}).get("technical")) or {}
        name = tech.get("name") or addon.get("invoiceName") or addon["planCode"].split(".")[0]
        if not include_windows and is_windows_flavor(str(name), tech):
            continue
        if not (tech.get("cpu") or {}).get("cores") and not tech.get("memory"):
            continue
        score = flavor_score(addon.get("planCode") or "", str(name), tech)
        prev = by_name.get(str(name))
        if not prev or score > prev[0]:
            by_name[str(name)] = (score, {"name": str(name), "technical": tech})
    return [item for _, (_, item) in sorted(by_name.items())]


def template_from_flavor(flavor: dict[str, Any]) -> dict[str, Any]:
    name = flavor["name"]
    tech = flavor["technical"]
    cpu = tech.get("cpu") or {}
    mem = tech.get("memory") or {}
    gpu = tech.get("gpu") or {}

    vcpus = int(cpu.get("cores") or 0)
    # OVH technical memory.size is GiB.
    ram_mib = int(round(float(mem.get("size") or 0) * 1024))
    archs = infer_archs(tech)

    gpu_count = int(gpu.get("number") or 0)
    gpu_model = gpu.get("model")
    parts = [
        f"OVHcloud {name} ({family_of(name)})",
        f"{vcpus} vCPU",
        f"{ram_mib} MiB RAM",
    ]
    if gpu_count and gpu_model:
        parts.append(f"{gpu_count}x {gpu_model}")
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

    if gpu_count and gpu_model:
        vendor, model = resolve_accelerator(str(gpu_model))
        if model == "0000":
            tmpl["info"]["description"] += "; unmapped accelerator model (review pci.model)"
        tmpl["pci"] = [
            {"vendor": vendor, "model": model, "quantity": gpu_count}
        ]
    return tmpl


def fetch_catalog(url: str) -> dict[str, Any]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "elemento-vm-templates/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, context=ctx, timeout=180) as resp:
        return json.load(resp)


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
            ("provider", "ovh", "count", str(len(entries))),
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
    flavors: list[dict[str, Any]], out_dir: Path, db_path: Path
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.json"):
        path.unlink()

    entries: list[dict[str, Any]] = []
    count = 0
    for flavor in flavors:
        tmpl = template_from_flavor(flavor)
        if not tmpl["cpu"]["slots"] or not tmpl["ram"]["ramsize"]:
            continue
        name = flavor["name"]
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
        "--catalog-url",
        default=DEFAULT_CATALOG_URL,
        help="OVH public cloud order catalog URL",
    )
    parser.add_argument(
        "--catalog-file",
        type=Path,
        default=None,
        help="Optional local OVH catalog JSON",
    )
    parser.add_argument(
        "--include-windows",
        action="store_true",
        help="Include win-* Windows-licensed flavor variants",
    )
    args = parser.parse_args()

    if args.catalog_file:
        catalog = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        used = f"file:{args.catalog_file}"
    else:
        catalog = fetch_catalog(args.catalog_url)
        used = args.catalog_url

    flavors = extract_flavors(catalog, include_windows=args.include_windows)
    n = write_templates(flavors, args.out_dir, args.db)
    print(
        f"Wrote {n} templates to {args.out_dir} and SQLite catalog {args.db} (source={used})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
