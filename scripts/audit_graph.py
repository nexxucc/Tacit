"""Audit graphify references against Python syntax; retain a lossless directed graph.

Run with the interpreter recorded in graphify-out/.graphify_python. Re-extraction
is AST/cache-only; uncached semantic documents stop the rebuild rather than vanish.
"""

import argparse
import ast
import copy
import json
import re
from collections import Counter
from pathlib import Path


def identifier(value):
    return re.sub(r"[^a-z0-9_]", "_", value.lower())


def imports(tree):
    result = {}
    # A nested function's imports do not bind names in the surrounding module.
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                result[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                result[alias.asname or alias.name] = node.module + "." + alias.name
    return result


def local_names(function):
    names = {a.arg for a in ast.walk(function.args) if isinstance(a, ast.arg)}
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        pending.extend(ast.iter_child_nodes(node))
    return names


def qualified(node, bindings):
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        parent = qualified(node.value, bindings)
        return parent + "." + node.attr if parent else None
    return None


def normalize(extraction, root):
    """Correct proven bindings; retain uncertain references and genuine recursion."""
    data = copy.deepcopy(extraction)
    nodes = {n["id"]: n for n in data["nodes"]}
    trees = {}
    corrections, recursions, unknown = [], [], []
    external_nodes = set()

    def tree_for(file):
        if file not in trees:
            path = Path(file)
            if not path.is_absolute():
                path = root / path
            trees[file] = ast.parse(path.read_text(encoding="utf-8"))
        return trees[file]

    def external(nid, name, kind):
        if nid not in nodes:
            nodes[nid] = {
                "id": nid,
                "label": name,
                "qualified_name": name,
                "file_type": "concept",
                "source_file": "",
                "source_location": None,
                "_origin": "ast",
                "external": True,
                "reference_kind": kind,
            }
            external_nodes.add(nid)
        return nid

    missing = {e["target"] for e in data["edges"] if e["target"] not in nodes}
    for target in sorted(missing):
        witnesses = [e for e in data["edges"] if e["target"] == target]
        modules = set()
        for edge in witnesses:
            if edge.get("relation") not in {"imports", "imports_from"}:
                continue
            file = edge.get("source_file", "")
            if not file.endswith(".py"):
                continue
            line = int(str(edge.get("source_location", "L0"))[1:])
            for node in ast.walk(tree_for(file)):
                if getattr(node, "lineno", None) != line:
                    continue
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module]
                    if isinstance(node, ast.ImportFrom) and not node.level and node.module
                    else []
                )
                modules.update(name for name in names if identifier(name) == target)
        if len(modules) == 1:
            external(target, modules.pop(), "imported_module")
        elif target.startswith("pkg_") and all(
            e.get("relation") == "depends_on" for e in witnesses
        ):
            external(target, target[4:].replace("_", "-"), "package_dependency")
        else:
            unknown.append({"id": target, "reason": "No unique import or package declaration"})

    for edge in data["edges"]:
        if edge.get("relation") != "calls" or edge["source"] != edge["target"]:
            continue
        file = edge.get("source_file", "")
        if not file.endswith(".py"):
            continue
        tree = tree_for(file)
        line = int(str(edge.get("source_location", "L0"))[1:])
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.lineno == line]
        scopes = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.lineno <= line <= n.end_lineno
        ]
        shadowed = local_names(max(scopes, key=lambda n: n.lineno)) if scopes else set()
        bindings = imports(tree)
        candidates = set()
        reason = None
        for call in calls:
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in bindings
                and call.func.id not in shadowed
            ):
                name = bindings[call.func.id]
                module, _, symbol = name.rpartition(".")
                suffix = module.replace(".", "/") + ".py"
                for n in nodes.values():
                    source = n.get("source_file", "")
                    if source.endswith(suffix) and n.get("label", "").lstrip(".") == symbol + "()":
                        candidates.add(n["id"])
                        reason = "import_binding"
            elif (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Call)
                and isinstance(call.func.value.func, ast.Name)
                and call.func.value.func.id == "super"
            ):
                classes = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.lineno <= line <= n.end_lineno
                ]
                if classes:
                    cls = max(classes, key=lambda n: n.lineno)
                    # Multiple inheritance needs a real MRO; don't guess its target.
                    if len(cls.bases) == 1:
                        base = qualified(cls.bases[0], bindings)
                        if base and "super" not in shadowed:
                            name = base + "." + call.func.attr
                            target = external("external_" + identifier(name), name, "base_method")
                            candidates.add(target)
                            reason = "super_dispatch"
        if len(candidates) == 1:
            old = edge["target"]
            edge["target"] = candidates.pop()
            edge["resolution"] = reason
            corrections.append(
                {
                    "source_file": file,
                    "source_location": edge.get("source_location"),
                    "old_target": old,
                    "new_target": edge["target"],
                    "reason": reason,
                }
            )
        elif not candidates:
            # Confirm a syntactic direct recursive call; don't excuse arbitrary loops.
            source = nodes[edge["source"]]
            functions = [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "L" + str(n.lineno) == source.get("source_location")
            ]
            if functions and any(
                isinstance(c.func, ast.Name)
                and c.func.id == functions[0].name
                and c.func.id not in shadowed
                for c in calls
            ):
                edge["recursion"] = True
                edge["resolution"] = "direct_recursive_call"
                recursions.append(
                    {
                        "source_file": file,
                        "source_location": edge.get("source_location"),
                        "node": edge["source"],
                    }
                )
            else:
                unknown.append({"edge": dict(edge), "reason": "Unverified self-call"})
    data["nodes"] = list(nodes.values())
    return data, {
        "external_nodes_added": len(external_nodes),
        "external_endpoint_modules": len(missing) - sum("id" in x for x in unknown),
        "corrected_calls": corrections,
        "verified_recursive_calls": recursions,
        "unresolved": unknown,
    }


def rebuild(root):
    from graphify.analyze import god_nodes, suggest_questions, surprising_connections
    from graphify.build import build_from_json
    from graphify.cache import check_semantic_cache
    from graphify.cluster import cluster, score_all
    from graphify.detect import detect, save_manifest
    from graphify.diagnostics import diagnose_extraction
    from graphify.export import to_json
    from graphify.extract import extract
    from graphify.report import generate

    out = root / "graphify-out"
    detection = detect(root)
    docs = [f for kind in ("document", "paper", "image") for f in detection["files"].get(kind, [])]
    spec = Path.home() / ".codex/skills/graphify/references/extraction-spec.md"
    n, e, h, uncached = check_semantic_cache(docs, root=str(root), prompt_file=str(spec))
    if uncached:
        raise SystemExit(
            "Run the graphify semantic workflow first; uncached documents: " + ", ".join(uncached)
        )
    result = extract(
        [Path(f) for f in detection["files"].get("code", [])], cache_root=root, root=root
    )
    result["nodes"].extend(n)
    result["edges"].extend(e)
    result["hyperedges"] = h
    for item in n + e:
        item.setdefault("_origin", "semantic")
    (out / "raw-extraction.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    before = diagnose_extraction(result, directed=True, root=root)
    fixed, audit = normalize(result, root)
    # The simple projection is only for clustering/report metrics. Export EVERY
    # corrected raw relationship, including parallel types and source locations.
    graph = build_from_json(fixed, directed=True, root=root)
    if not graph:
        raise SystemExit("Empty extraction; refusing to replace graph")
    communities = cluster(graph)
    cohesion = score_all(graph, communities)
    old = json.loads((out / "graph.json").read_text())
    old_names = {n["id"]: n.get("community_name") for n in old["nodes"]}
    labels = {}
    for cid, members in communities.items():
        votes = Counter(old_names[n] for n in members if old_names.get(n))
        labels[cid] = votes.most_common(1)[0][0] if votes else "External References"
    questions = suggest_questions(graph, communities, labels)
    report = generate(
        graph,
        communities,
        cohesion,
        labels,
        god_nodes(graph),
        surprising_connections(graph, communities),
        detection,
        {"input": 0, "output": 0},
        str(root),
        suggested_questions=questions,
    )
    report = report.replace(
        "- Token cost: 0 input · 0 output",
        "- Token cost: this AST/cache rebuild uses no model calls; earlier semantic token usage is unmeasured.",
    )
    report += (
        "\n## Audited Relationship Storage\n\n"
        "The JSON retains every directed relationship and its source location. "
        "Clustering and hub metrics use a simple projection. External modules are explicit "
        "reference nodes, not missing Tacit implementations. "
        f"{len(audit['verified_recursive_calls'])} direct recursive calls were verified "
        "against Python syntax (including the audit helper itself). Corrected imported/base "
        "calls are listed in `audit.json`. Repeated endpoint pairs reported by the generic "
        "diagnostic describe a hypothetical lossy simple projection; the exported multigraph "
        "keeps them. Inferred graph relationships remain hypotheses, not verified runtime calls.\n"
    )
    if not to_json(
        graph,
        communities,
        str(out / "graph.json"),
        community_labels=labels,
        original_links=fixed["edges"],
    ):
        raise SystemExit("Graphify shrink guard refused export")
    saved = json.loads((out / "graph.json").read_text())
    if len(saved["links"]) != len(result["edges"]):
        raise SystemExit(
            "Relationship loss detected; inspect raw-extraction.json before proceeding"
        )
    report = re.sub(
        r"^- (\d+) nodes · \d+ edges ·",
        rf"- \1 nodes · {len(saved['links'])} directed relationships ·",
        report,
        flags=re.MULTILINE,
    )
    # Diagnose serialized data: parallel-edge groups are retained because the
    # artifact declares multigraph=True; a hypothetical simple projection is lossy.
    after = diagnose_extraction(saved, directed=True, root=root)
    audit.update(
        raw_edges=len(result["edges"]),
        exported_edges=len(saved["links"]),
        exported_directed=saved["directed"],
        exported_multigraph=saved["multigraph"],
        dangling_after=after["dangling_endpoint_edges"],
        parallel_relationships_preserved=before["directed_same_endpoint_collapsed_edges"],
        actual_relationship_loss=0,
    )
    after["exported_multigraph"] = saved["multigraph"]
    after["actual_relationship_loss"] = 0
    after["verified_recursive_calls"] = len(audit["verified_recursive_calls"])
    after["diagnostic_note"] = (
        "Same-endpoint collision counters describe a simple projection; graph.json retains parallel directed links."
    )
    (out / "audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    (out / "diagnostics-before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    (out / "diagnostics.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    (out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    (out / ".graphify_labels.json").write_text(
        json.dumps({str(k): v for k, v in labels.items()}), encoding="utf-8"
    )
    save_manifest(
        detection["files"],
        root=str(root),
        scan_corpus={f for files in detection["files"].values() for f in files},
    )
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    rebuild(parser.parse_args().root.resolve())
