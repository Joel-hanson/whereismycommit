#!/usr/bin/env python3
"""Build stored dependency graphs for Strimzi releases (last N years).

Sources (no image pull required):
  - GitHub releases for strimzi/strimzi-kafka-operator
  - kafka-versions.yaml → supported Kafka image tags
  - docker-images/artifacts/kafka-thirdparty-libs/*/pom.xml → pinned plugins
  - LinkedIn Cruise Control Maven POM → cruise-control package tree
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = "strimzi/strimzi-kafka-operator"
RAW = f"https://raw.githubusercontent.com/{REPO}"
API = f"https://api.github.com/repos/{REPO}"
CC_REPO = "https://linkedin.jfrog.io/artifactory/cruise-control"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}

UA = "whereismycommit-strimzi-scanner/0.1"


def fetch(url: str, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last_err: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}: {last_err}")


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", errors="replace")


def fetch_json(url: str):
    return json.loads(fetch_text(url))


def parse_pom_xml(text: str) -> ET.Element:
    # Strip default xmlns so XPath stays simple
    text = re.sub(r'\sxmlns="[^"]+"', "", text, count=1)
    return ET.fromstring(text)


def pom_properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in root.findall("./properties/*"):
        if prop.tag and prop.text:
            props[prop.tag] = prop.text.strip()
    return props


def resolve_prop(value: str | None, props: dict[str, str]) -> str | None:
    if value is None:
        return None
    m = re.fullmatch(r"\$\{([^}]+)\}", value.strip())
    if m:
        return props.get(m.group(1), value)
    return value


def pom_dependencies(root: ET.Element, props: dict[str, str]) -> list[dict]:
    out = []
    for dep in root.findall("./dependencies/dependency"):
        group = (dep.findtext("groupId") or "").strip()
        artifact = (dep.findtext("artifactId") or "").strip()
        version = resolve_prop(dep.findtext("version"), props)
        scope = (dep.findtext("scope") or "compile").strip()
        if not group or not artifact:
            continue
        out.append(
            {
                "group": group,
                "name": artifact,
                "version": version or "unknown",
                "scope": scope,
                "purl": f"pkg:maven/{group}/{artifact}@{version or 'unknown'}",
            }
        )
    return out


def parse_kafka_versions(yaml_text: str) -> list[dict]:
    """Minimal YAML subset parser for kafka-versions.yaml entries."""
    versions: list[dict] = []
    current: dict | None = None
    for raw in yaml_text.splitlines():
        if raw.startswith("- version:"):
            if current:
                versions.append(current)
            current = {"version": raw.split(":", 1)[1].strip()}
            continue
        if current is None:
            continue
        if raw.startswith("- "):
            versions.append(current)
            current = None
            continue
        m = re.match(r"^\s+([\w-]+):\s*(.*)$", raw)
        if m:
            val = m.group(2).strip()
            # Strip YAML inline comments
            if " #" in val:
                val = val.split(" #", 1)[0].rstrip()
            elif val.startswith("#"):
                continue
            current[m.group(1)] = val
    if current:
        versions.append(current)
    return [v for v in versions if v.get("supported") == "true"]


def list_releases(years: float) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(years * 365.25))
    releases: list[dict] = []
    page = 1
    while True:
        batch = fetch_json(f"{API}/releases?per_page=100&page={page}")
        if not batch:
            break
        for rel in batch:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel.get("tag_name") or ""
            published = rel.get("published_at") or rel.get("created_at")
            if not tag or not published:
                continue
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if dt < cutoff:
                return releases
            releases.append({"release": tag, "publishedAt": published})
        page += 1
        if page > 20:
            break
    return releases


def list_thirdparty_dirs(tag: str) -> list[str]:
    """List thirdparty lib folders without a full recursive git tree walk."""
    url = (
        f"{API}/contents/docker-images/artifacts/kafka-thirdparty-libs?ref={tag}"
    )
    try:
        items = fetch_json(url)
        dirs = [i["name"] for i in items if i.get("type") == "dir"]
        return sorted(dirs) or ["cc"]
    except Exception:
        return ["cc"]


def fetch_cc_packages(version: str, cache: dict[str, list[dict]]) -> list[dict]:
    if version in cache:
        return cache[version]
    url = (
        f"{CC_REPO}/com/linkedin/cruisecontrol/cruise-control/"
        f"{version}/cruise-control-{version}.pom"
    )
    try:
        root = parse_pom_xml(fetch_text(url))
        props = pom_properties(root)
        deps = pom_dependencies(root, props)
    except Exception as err:  # noqa: BLE001
        print(f"  warn: cruise-control {version} pom: {err}", file=sys.stderr)
        deps = []
    cache[version] = deps
    return deps


def build_release(tag: str, published_at: str, tp_dirs: list[str], cc_cache: dict) -> dict:
    kv_text = fetch_text(f"{RAW}/{tag}/kafka-versions.yaml")
    kafka_versions = parse_kafka_versions(kv_text)

    components: list[dict] = []
    packages: list[dict] = []
    nodes: list[dict] = [
        {"id": f"release:{tag}", "label": f"Strimzi {tag}", "kind": "release"}
    ]
    edges: list[list[str]] = []

    # Prefer default Kafka's third-party-libs, then others, then cc
    ordered_libs: list[str] = []
    default_lib = next(
        (v.get("third-party-libs") for v in kafka_versions if v.get("default") == "true"),
        None,
    )
    if default_lib:
        ordered_libs.append(default_lib)
    for v in kafka_versions:
        key = v.get("third-party-libs")
        if key and key not in ordered_libs:
            ordered_libs.append(key)
    # Default Kafka third-party-libs + cc is enough for a release-level graph
    pom_keys: list[str] = []
    if default_lib:
        pom_keys.append(default_lib)
    elif ordered_libs:
        pom_keys.append(ordered_libs[0])
    if "cc" not in pom_keys:
        pom_keys.append("cc")
    if tp_dirs:
        # Keep only dirs that exist for this tag
        filtered = [k for k in pom_keys if k in tp_dirs]
        pom_keys = filtered or (["cc"] if "cc" in tp_dirs else pom_keys)

    seen_component: set[str] = set()
    cruise_control_version: str | None = None

    for key in pom_keys:
        pom_url = f"{RAW}/{tag}/docker-images/artifacts/kafka-thirdparty-libs/{key}/pom.xml"
        try:
            root = parse_pom_xml(fetch_text(pom_url))
        except Exception as err:  # noqa: BLE001
            print(f"  warn: missing pom {key}: {err}", file=sys.stderr)
            continue
        props = pom_properties(root)
        deps = pom_dependencies(root, props)

        # Promote interesting version pins from properties into components
        prop_map = {
            "cruise-control.version": ("cruise-control", "com.linkedin.cruisecontrol"),
            "strimzi-oauth.version": ("strimzi-oauth", "io.strimzi"),
            "strimzi-metrics-reporter.version": ("strimzi-metrics-reporter", "io.strimzi"),
            "kafka-quotas-plugin.version": ("kafka-quotas-plugin", "io.strimzi"),
            "kafka-kubernetes-config-provider.version": (
                "kafka-kubernetes-config-provider",
                "io.strimzi",
            ),
            "opentelemetry.version": ("opentelemetry", "io.opentelemetry"),
            "prometheus.version": ("prometheus-jmx", "io.prometheus"),
            "log4j.version": ("log4j", "org.apache.logging.log4j"),
            "commons-beanutils.version": ("commons-beanutils", "commons-beanutils"),
        }
        for prop, (name, group) in prop_map.items():
            if prop not in props:
                continue
            cid = f"component:{name}"
            if cid not in seen_component:
                seen_component.add(cid)
                components.append(
                    {
                        "id": name,
                        "name": name,
                        "group": group,
                        "version": props[prop],
                        "source": f"kafka-thirdparty-libs/{key}/pom.xml",
                    }
                )
                nodes.append(
                    {
                        "id": cid,
                        "label": f"{name}@{props[prop]}",
                        "kind": "component",
                        "version": props[prop],
                    }
                )
            if name == "cruise-control":
                cruise_control_version = props[prop]

        for dep in deps:
            packages.append(
                {
                    **dep,
                    "via": [f"thirdparty:{key}"],
                    "direct": True,
                }
            )

    images = []
    for kv in kafka_versions:
        kver = kv["version"]
        image_tag = f"{tag}-kafka-{kver}"
        iid = f"image:kafka-{kver}"
        images.append(
            {
                "name": "kafka",
                "registry": "quay.io/strimzi/kafka",
                "tag": image_tag,
                "kafkaVersion": kver,
                "thirdPartyLibs": kv.get("third-party-libs"),
                "default": kv.get("default") == "true",
            }
        )
        nodes.append(
            {
                "id": iid,
                "label": f"kafka {kver}",
                "kind": "image",
                "tag": image_tag,
            }
        )
        edges.append([f"release:{tag}", iid])
        for comp in components:
            edges.append([iid, f"component:{comp['id']}"])

    if cruise_control_version:
        cc_deps = fetch_cc_packages(cruise_control_version, cc_cache)
        for dep in cc_deps:
            packages.append({**dep, "via": ["cruise-control"], "direct": False})
            pid = f"pkg:{dep['group']}:{dep['name']}@{dep['version']}"
            if not any(n["id"] == pid for n in nodes):
                nodes.append(
                    {
                        "id": pid,
                        "label": f"{dep['name']}@{dep['version']}",
                        "kind": "package",
                        "group": dep["group"],
                        "version": dep["version"],
                    }
                )
            edges.append(["component:cruise-control", pid])

    # De-dupe packages by purl, merge via
    merged: dict[str, dict] = {}
    for pkg in packages:
        key = pkg["purl"]
        if key not in merged:
            merged[key] = {**pkg, "via": list(pkg.get("via") or [])}
        else:
            for v in pkg.get("via") or []:
                if v not in merged[key]["via"]:
                    merged[key]["via"].append(v)

    return {
        "product": "strimzi",
        "repo": REPO,
        "release": tag,
        "publishedAt": published_at,
        "scannedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "maven-poms",
        "images": images,
        "components": components,
        "packages": sorted(merged.values(), key=lambda p: (p["name"], p["version"])),
        "nodes": nodes,
        "edges": edges,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "strimzi",
    )
    parser.add_argument("--limit", type=int, default=0, help="Scan only first N releases")
    args = parser.parse_args()

    out: Path = args.out
    releases_dir = out / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    print(f"Listing Strimzi releases (last {args.years} years)…")
    releases = list_releases(args.years)
    if args.limit:
        releases = releases[: args.limit]
    print(f"Found {len(releases)} releases")

    shared_tp_dirs: list[str] | None = None
    cc_cache: dict[str, list[dict]] = {}
    index_entries = []

    for i, rel in enumerate(releases, 1):
        tag = rel["release"]
        print(f"[{i}/{len(releases)}] {tag}")
        # Always list dirs for this tag so folder names match the release
        try:
            tp_dirs = list_thirdparty_dirs(tag)
            shared_tp_dirs = tp_dirs
        except Exception as err:  # noqa: BLE001
            print(f"  warn: dirs {tag}: {err}", file=sys.stderr)
            tp_dirs = shared_tp_dirs or ["cc"]
        try:
            graph = build_release(tag, rel["publishedAt"], tp_dirs, cc_cache)
        except Exception as err:  # noqa: BLE001
            print(f"  error: {err}", file=sys.stderr)
            continue
        path = releases_dir / f"{tag}.json"
        path.write_text(json.dumps(graph, indent=2) + "\n")
        cc = next((c["version"] for c in graph["components"] if c["id"] == "cruise-control"), None)
        index_entries.append(
            {
                "release": tag,
                "publishedAt": rel["publishedAt"],
                "file": f"releases/{tag}.json",
                "cruiseControl": cc,
                "kafkaVersions": [img["kafkaVersion"] for img in graph["images"]],
                "packageCount": len(graph["packages"]),
                "componentCount": len(graph["components"]),
            }
        )
        time.sleep(0.2)  # be kind to GitHub raw + artifactory

    index = {
        "product": "strimzi",
        "repo": REPO,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowYears": args.years,
        "releaseCount": len(index_entries),
        "releases": index_entries,
    }
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"Wrote {len(index_entries)} graphs → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
