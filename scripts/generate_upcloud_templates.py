#!/usr/bin/env python3
"""Generate Elemento VM templates from UpCloud server plans."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

NVIDIA_VENDOR = "10de"
NVIDIA_GPU_MODEL_IDS: dict[str, str] = {
    "l4": "27b8",
    "l40s": "26b9",
    "l40": "26b5",
    "h100": "2330",
    "h200": "2335",
    "b200": "2901",
    "b300": "3182",
    "a100": "20b0",
    "t4": "1eb8",
    "v100": "1db4",
}

PLAN_RE = re.compile(
    r"^(?:GPU-)?(?P<cores>\d+)xCPU-(?P<mem_gb>\d+)GB(?:-(?P<gpu_count>\d+)x(?P<gpu_model>.+))?$"
)
DOCS_PLAN_RE = re.compile(r"\b(?:GPU-)?\d+xCPU-\d+GB(?:-\d+x[A-Za-z0-9]+)?\b")
DOCS_URLS = [
    "https://upcloud.com/docs/products/cloud-servers/configurations/",
    "https://upcloud.com/docs/products/gpu-servers/configurations/",
]
API_PLAN_URL = "https://api.upcloud.com/1.3/plan"


def family_of(name: str) -> str:
    return "GPU" if name.startswith("GPU-") else "CLOUD"


def resolve_nvidia_model(gpu_name: str) -> str:
    name = gpu_name.strip().lower().replace("nvidia ", "").replace("nvidia-", "")
    if name in NVIDIA_GPU_MODEL_IDS:
        return NVIDIA_GPU_MODEL_IDS[name]
    for key, model in sorted(NVIDIA_GPU_MODEL_IDS.items(), key=lambda kv: -len(kv[0])):
        if key in name:
            return model
    return "0000"


def cpu_flags(archs: list[str]) -> list[str]:
    if any(a in ("X86_64", "X86") for a in archs):
        return ["sse2", "avx2"]
    return []


def parse_plan_name(name: str) -> dict[str, Any] | None:
    m = PLAN_RE.match(name)
    if not m:
        return None
    cores = int(m.group("cores"))
    mem_gb = int(m.group("mem_gb"))
    gpu_count = int(m.group("gpu_count") or 0)
    gpu_model = m.group("gpu_model")
    return {
        "name": name,
        "core_number": cores,
        "memory_amount": mem_gb * 1024,
        "gpu_amount": gpu_count,
        "gpu_model": f"NVIDIA {gpu_model}" if gpu_model else None,
    }


def template_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    name = plan["name"]
    vcpus = int(plan["core_number"])
    ram_mib = int(plan["memory_amount"])
    archs = ["X86_64"]
    gpu_count = int(plan.get("gpu_amount") or 0)
    gpu_model = plan.get("gpu_model")

    parts = [
        f"UpCloud {name} ({family_of(name)})",
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
        model = resolve_nvidia_model(str(gpu_model))
        if model == "0000":
            tmpl["info"]["description"] += "; unmapped NVIDIA GPU model (review pci.model)"
        tmpl["pci"] = [
            {"vendor": NVIDIA_VENDOR, "model": model, "quantity": gpu_count}
        ]
    return tmpl


def fetch_via_api() -> list[dict[str, Any]]:
    user = os.environ.get("UPCLOUD_USERNAME") or os.environ.get("UPCLOUD_API_USER")
    password = os.environ.get("UPCLOUD_PASSWORD") or os.environ.get("UPCLOUD_API_PASSWORD")
    if not user or not password:
        raise RuntimeError("UPCLOUD_USERNAME/UPCLOUD_PASSWORD not set")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        API_PLAN_URL,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "elemento-vm-templates/1.0",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        payload = json.load(resp)
    plans = payload.get("plans", {}).get("plan") or []
    if not isinstance(plans, list) or not plans:
        raise RuntimeError("Empty UpCloud API plan list")
    return plans


def fetch_via_docs() -> list[dict[str, Any]]:
    ctx = ssl.create_default_context()
    names: set[str] = set()
    for url in DOCS_URLS:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, context=ctx, timeout=90) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        names.update(DOCS_PLAN_RE.findall(html))
    plans: list[dict[str, Any]] = []
    for name in sorted(names):
        parsed = parse_plan_name(name)
        if parsed:
            plans.append(parsed)
    if not plans:
        raise RuntimeError("No UpCloud plans parsed from docs")
    return plans


def load_catalog(source: str) -> tuple[list[dict[str, Any]], str]:
    errors: list[str] = []
    if source in ("auto", "api"):
        try:
            return fetch_via_api(), "upcloud-api"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"api: {exc}")
            if source == "api":
                raise RuntimeError("; ".join(errors)) from exc
    if source in ("auto", "docs"):
        try:
            return fetch_via_docs(), "upcloud-docs"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"docs: {exc}")
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
            ("provider", "upcloud", "count", str(len(entries))),
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
    plans: list[dict[str, Any]], out_dir: Path, db_path: Path
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.json"):
        path.unlink()

    # Deduplicate by plan name.
    by_name: dict[str, dict[str, Any]] = {}
    for plan in plans:
        name = plan.get("name")
        if not name:
            continue
        # Prefer richer API records; still normalize missing GPU fields from name.
        if name not in by_name:
            parsed = parse_plan_name(name)
            merged = dict(parsed or {})
            merged.update({k: v for k, v in plan.items() if v is not None})
            if not merged.get("core_number") and parsed:
                merged["core_number"] = parsed["core_number"]
            if not merged.get("memory_amount") and parsed:
                merged["memory_amount"] = parsed["memory_amount"]
            if not merged.get("gpu_amount") and parsed:
                merged["gpu_amount"] = parsed["gpu_amount"]
            if not merged.get("gpu_model") and parsed:
                merged["gpu_model"] = parsed["gpu_model"]
            by_name[name] = merged

    entries: list[dict[str, Any]] = []
    count = 0
    for name in sorted(by_name):
        plan = by_name[name]
        if not int(plan.get("core_number") or 0):
            continue
        tmpl = template_from_plan(plan)
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
        "--source",
        choices=("auto", "api", "docs"),
        default="auto",
        help="Catalog source (auto tries API then public docs)",
    )
    parser.add_argument(
        "--catalog-file",
        type=Path,
        default=None,
        help="Optional local UpCloud plans JSON ({'plans':{'plan':[...]}} or list)",
    )
    args = parser.parse_args()

    if args.catalog_file:
        raw = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "plans" in raw:
            plans = list(raw["plans"].get("plan") or [])
        elif isinstance(raw, list):
            plans = raw
        else:
            raise SystemExit(f"Unrecognized catalog format in {args.catalog_file}")
        used = f"file:{args.catalog_file}"
    else:
        plans, used = load_catalog(args.source)

    n = write_templates(plans, args.out_dir, args.db)
    print(
        f"Wrote {n} templates to {args.out_dir} and SQLite catalog {args.db} (source={used})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
