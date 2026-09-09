#!/usr/bin/env python3
"""Generate Elemento VM templates from Azure Virtual Machine sizes."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any

NVIDIA_VENDOR = "10de"
AMD_VENDOR = "1002"
INTEL_VENDOR = "8086"
HABANA_VENDOR = "1da3"
QUALCOMM_VENDOR = "17cb"
XILINX_VENDOR = "10ee"
UNKNOWN_VENDOR = "0000"

# Lowercase hex device IDs keyed by model token.
ACCELERATOR_MODEL_IDS: dict[str, tuple[str, str]] = {
    # NVIDIA (10de)
    "k80": (NVIDIA_VENDOR, "102d"),
    "p100": (NVIDIA_VENDOR, "15f8"),
    "p40": (NVIDIA_VENDOR, "1b38"),
    "p4": (NVIDIA_VENDOR, "1bb3"),
    "v100": (NVIDIA_VENDOR, "1db4"),
    "t4": (NVIDIA_VENDOR, "1eb8"),
    "a10": (NVIDIA_VENDOR, "2236"),
    "a100": (NVIDIA_VENDOR, "20b0"),
    "m60": (NVIDIA_VENDOR, "13f2"),
    "l4": (NVIDIA_VENDOR, "27b8"),
    "l40s": (NVIDIA_VENDOR, "26b9"),
    "h100": (NVIDIA_VENDOR, "2330"),
    "h200": (NVIDIA_VENDOR, "2335"),
    "rtx 6000": (NVIDIA_VENDOR, "26b1"),
    "rtx6k": (NVIDIA_VENDOR, "26b1"),
    "rtx6000": (NVIDIA_VENDOR, "26b1"),
    # AMD (1002)
    "mi25": (AMD_VENDOR, "740c"),
    "instinct mi25": (AMD_VENDOR, "740c"),
    "v620": (AMD_VENDOR, "73a3"),
    "radeon pro v620": (AMD_VENDOR, "73a3"),
    "v710": (AMD_VENDOR, "744c"),
    "radeon pro v710": (AMD_VENDOR, "744c"),
    # Habana (1da3)
    "gaudi": (HABANA_VENDOR, "1000"),
    "habana": (HABANA_VENDOR, "1000"),
    # Xilinx / AMD Alveo (10ee)
    "ma35d": (XILINX_VENDOR, "0000"),
    "alveo ma35d": (XILINX_VENDOR, "0000"),
    "asic vpu": (XILINX_VENDOR, "0000"),
}

VANTAGE_AZURE_URL = "https://instances.vantage.sh/azure/instances.json"
GPU_COUNT_MODEL_RE = re.compile(
    r"(?i)^\s*(?P<count>\d+)\s*[xX]\s*(?P<model>.+?)\s*$"
)
GPU_COUNT_SPACE_RE = re.compile(r"(?i)^\s*(?P<count>\d+)\s+(?P<model>.+?)\s*$")
GPU_FRAC_RE = re.compile(
    r"(?i)^\s*(?P<num>\d+)\s*/\s*(?P<den>\d+)\s*[xX]?\s*(?P<model>.*?)\s*$"
)


def family_of(sku_name: str, fallback: str | None = None) -> str:
    # Standard_D2s_v5 -> Dsv5-ish; prefer provided family when present.
    if fallback:
        return fallback
    body = sku_name.removeprefix("Standard_").removeprefix("Basic_")
    return body.split("_", 1)[0]


def azure_sku_name(item: dict[str, Any]) -> str:
    pretty = (
        item.get("pretty_name_azure")
        or item.get("pretty_name")
        or item.get("instance_type")
        or ""
    )
    size = re.sub(r"\s+", "_", str(pretty).strip())
    if not size.lower().startswith(("standard_", "basic_")):
        size = f"Standard_{size}"
    # Normalize Standard_standard_...
    size = re.sub(r"(?i)^Standard_Standard_", "Standard_", size)
    return size


def map_archs(archs: list[Any] | None) -> list[str]:
    mapped: list[str] = []
    for arch in archs or []:
        key = str(arch).lower().replace("-", "")
        if key in ("x64", "x86_64", "amd64"):
            mapped.append("X86_64")
        elif key in ("arm64", "aarch64"):
            mapped.append("AARCH64")
        elif key:
            mapped.append(str(arch).upper())
    # Deduplicate
    out: list[str] = []
    for a in mapped:
        if a not in out:
            out.append(a)
    return out or ["X86_64"]


def cpu_flags(archs: list[str]) -> list[str]:
    if any(a in ("X86_64", "X86") for a in archs):
        return ["sse2", "avx2"]
    return []


def normalize_accel_label(label: str) -> str:
    name = label.strip().lower()
    name = re.sub(r"\(.*?\)", "", name).strip()
    name = name.replace("nvidia ", "").replace("amd ", "").replace("radeon pro ", "")
    name = name.replace("80gb ", "").replace("nvlink", "").strip()
    name = re.sub(r"\s+", " ", name)
    return name


def resolve_accelerator(label: str | None, sku_name: str = "") -> tuple[str, str, str]:
    """Return (vendor, model_hex, display_name) for any accelerator."""
    sku = sku_name.lower()
    display = (label or "").strip() or "GPU"
    blob = normalize_accel_label(f"{label or ''} {sku_name}")

    # SKU-based vendor hints when the GPU field is only a count.
    if "ma35d" in blob or "alveo" in blob or ("asic" in blob and "vpu" in blob):
        display = display if label else "Alveo MA35D"
        return XILINX_VENDOR, "0000", display
    if re.search(r"standard_nm\d+ads_ma35d", sku):
        display = display if label else "Alveo MA35D"
        return XILINX_VENDOR, "0000", display
    if re.search(r"standard_nv\d+as_v4", sku) or "mi25" in blob:
        display = display if label else "MI25"
        return AMD_VENDOR, ACCELERATOR_MODEL_IDS["mi25"][1], display
    if re.search(r"standard_ng\d+.*v620", sku) or "v620" in blob:
        display = display if label else "Radeon Pro V620"
        return AMD_VENDOR, ACCELERATOR_MODEL_IDS["v620"][1], display
    if "v710" in blob:
        display = display if label else "Radeon Pro V710"
        return AMD_VENDOR, ACCELERATOR_MODEL_IDS["v710"][1], display
    if "rtx6k" in sku or "rtx6k" in blob or "rtx 6000" in blob:
        display = display if label and label.lower() != "gpu" else "RTX 6000 Ada"
        return NVIDIA_VENDOR, ACCELERATOR_MODEL_IDS["rtx6k"][1], display

    for key, (vendor, model) in sorted(
        ACCELERATOR_MODEL_IDS.items(), key=lambda kv: -len(kv[0])
    ):
        if key in blob:
            return vendor, model, display

    # Generic vendor inference from keywords.
    if any(tok in blob for tok in ("amd", "radeon", "instinct", "mi25", "v620", "v710")):
        return AMD_VENDOR, "0000", display
    if any(tok in blob for tok in ("habana", "gaudi")):
        return HABANA_VENDOR, "0000", display
    if "qualcomm" in blob or "ai 100" in blob:
        return QUALCOMM_VENDOR, "0000", display
    if "intel" in blob or "asic" in blob or "vpu" in blob:
        return INTEL_VENDOR if "intel" in blob else UNKNOWN_VENDOR, "0000", display
    if any(
        tok in blob
        for tok in (
            "nvidia",
            "tesla",
            "geforce",
            "quadro",
            "a100",
            "h100",
            "h200",
            "l4",
            "l40",
            "t4",
            "v100",
            "a10",
            "k80",
            "p100",
            "p40",
            "m60",
        )
    ):
        return NVIDIA_VENDOR, "0000", display

    # Azure GPU series without a parsed model: assume NVIDIA for NC/ND/NV* (non-AMD) families.
    if re.search(r"standard_n[cd]", sku) or re.search(r"standard_nv(?!\d+as_v4)", sku):
        return NVIDIA_VENDOR, "0000", display

    return UNKNOWN_VENDOR, "0000", display


def parse_gpu_field(raw: Any) -> tuple[int, str | None]:
    s = str(raw or "").strip()
    if not s or s in ("0", "0.0", "None", "null"):
        return 0, None

    m = GPU_COUNT_MODEL_RE.match(s)
    if m:
        return int(m.group("count")), m.group("model").strip()

    m = GPU_FRAC_RE.match(s)
    if m:
        model = (m.group("model") or "").strip() or "fractional GPU"
        # Clean leading artifacts like "th MI25" from "1/8X th MI25"
        model = re.sub(r"(?i)^\s*(th|the)\s+", "", model).strip()
        return 1, f"{model} (fractional {m.group('num')}/{m.group('den')})"

    m = GPU_COUNT_SPACE_RE.match(s)
    if m and not m.group("model").replace(".", "", 1).isdigit():
        return int(m.group("count")), m.group("model").strip()

    try:
        val = float(s)
    except ValueError:
        return 0, None
    if val <= 0:
        return 0, None
    if val >= 1 and abs(val - round(val)) < 1e-9:
        return int(round(val)), None
    return 1, f"fractional GPU ({s})"


def template_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    name = azure_sku_name(item)
    vcpus = int(item.get("vcpus_available") or item.get("vcpu") or 0)
    if not vcpus:
        return None
    memory_gb = float(item.get("memory") or 0)
    ram_mib = int(round(memory_gb * 1024))
    if ram_mib <= 0:
        return None
    archs = map_archs(item.get("arch") if isinstance(item.get("arch"), list) else None)
    family = str(item.get("family") or family_of(name))
    category = item.get("category") or "general"

    gpu_count, gpu_model = parse_gpu_field(item.get("GPU"))
    parts = [
        f"Azure {name} ({family}/{category})",
        f"{vcpus} vCPU",
        f"{ram_mib} MiB RAM",
    ]
    if gpu_count and gpu_model:
        parts.append(f"{gpu_count}x {gpu_model}")
    elif gpu_count:
        parts.append(f"{gpu_count}x GPU")
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

    if gpu_count:
        vendor, model, display = resolve_accelerator(gpu_model, name)
        if gpu_model is None and display and display != "GPU":
            tmpl["info"]["description"] = tmpl["info"]["description"].replace(
                f"{gpu_count}x GPU", f"{gpu_count}x {display}"
            )
        if model == "0000":
            tmpl["info"]["description"] += (
                f"; unmapped accelerator model vendor={vendor} (review pci.model)"
            )
        tmpl["pci"] = [
            {"vendor": vendor, "model": model, "quantity": gpu_count}
        ]
    return tmpl


def fetch_via_vantage() -> list[dict[str, Any]]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        VANTAGE_AZURE_URL,
        headers={"User-Agent": "elemento-vm-templates-generator/1.0"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=180) as resp:
        payload = json.load(resp)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Azure vantage catalog format")
    return payload


def index_entry(tmpl: dict[str, Any], rel_file: str, family: str) -> dict[str, Any]:
    name = tmpl["info"]["name"]
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
            ("provider", "azure", "count", str(len(entries))),
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


def write_templates(items: list[dict[str, Any]], out_dir: Path, db_path: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.json"):
        path.unlink()

    cleaned: dict[str, tuple[dict[str, Any], dict[str, Any], int]] = {}
    for item in items:
        tmpl = template_from_item(item)
        if not tmpl:
            continue
        name = tmpl["info"]["name"]
        score = (1 if item.get("arch") else 0) + (1 if item.get("vcpus_available") else 0)
        prev = cleaned.get(name)
        if not prev or score >= prev[2]:
            cleaned[name] = (item, tmpl, score)

    entries: list[dict[str, Any]] = []
    count = 0
    for name in sorted(cleaned):
        item, tmpl, _ = cleaned[name]
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(tmpl, indent=4) + "\n", encoding="utf-8")
        family = str(item.get("family") or family_of(name))
        entries.append(index_entry(tmpl, f"templates/{name}.json", family))
        count += 1

    write_sqlite(entries, db_path)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    parser.add_argument("--out-dir", type=Path, default=root / "templates")
    parser.add_argument("--db", type=Path, default=root / "index.sqlite")
    parser.add_argument(
        "--source",
        choices=("auto", "vantage"),
        default="auto",
        help="Catalog source",
    )
    parser.add_argument(
        "--catalog-file",
        type=Path,
        default=None,
        help="Optional local Azure instances JSON list",
    )
    args = parser.parse_args()

    if args.catalog_file:
        items = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            raise SystemExit(f"Unrecognized catalog format in {args.catalog_file}")
        used = f"file:{args.catalog_file}"
    else:
        items = fetch_via_vantage()
        used = "vantage"

    n = write_templates(items, args.out_dir, args.db)
    print(
        f"Wrote {n} templates to {args.out_dir} and SQLite catalog {args.db} (source={used})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
