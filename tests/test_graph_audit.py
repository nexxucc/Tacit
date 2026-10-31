"""Regression cases for graph warnings that must not become application changes."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "audit_graph", Path(__file__).resolve().parents[1] / "scripts/audit_graph.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def node(nid, label, file="fixture.py", line=1):
    return {"id": nid, "label": label, "source_file": file, "source_location": f"L{line}"}


def edge(source, target, relation="calls", line=1):
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "source_file": "fixture.py",
        "source_location": f"L{line}",
    }


def test_missing_import_nodes_are_explicit_external_references(tmp_path):
    (tmp_path / "fixture.py").write_text("import json\nfrom sqlalchemy.orm import Session\n")
    original = {
        "nodes": [node("module", "fixture.py")],
        "edges": [
            edge("module", "json", "imports", 1),
            edge("module", "sqlalchemy_orm", "imports_from", 2),
        ],
    }
    fixed, result = audit.normalize(original, tmp_path)
    by_id = {n["id"]: n for n in fixed["nodes"]}
    assert by_id["json"]["external"] is True
    assert by_id["sqlalchemy_orm"]["qualified_name"] == "sqlalchemy.orm"
    assert result["external_endpoint_modules"] == 2
    assert not result["unresolved"]
    assert len(original["nodes"]) == 1  # Raw evidence remains unchanged.


def test_unproven_internal_endpoint_is_not_hidden(tmp_path):
    (tmp_path / "fixture.py").write_text("import json\n")
    fixed, result = audit.normalize(
        {"nodes": [node("module", "fixture.py")], "edges": [edge("module", "missing_internal")]},
        tmp_path,
    )
    assert "missing_internal" not in {n["id"] for n in fixed["nodes"]}
    assert result["unresolved"][0]["id"] == "missing_internal"


def test_imported_function_is_not_enclosing_method_recursion(tmp_path):
    (tmp_path / "fixture.py").write_text(
        "from pkg.rules import evaluate\nclass Service:\n    def evaluate(self):\n        return evaluate()\n"
    )
    original = {
        "nodes": [
            node("method", ".evaluate()", line=3),
            node("function", "evaluate()", "pkg/rules.py"),
        ],
        "edges": [edge("method", "method", line=4)],
    }
    fixed, result = audit.normalize(original, tmp_path)
    assert fixed["edges"][0]["target"] == "function"
    assert result["corrected_calls"][0]["reason"] == "import_binding"
    assert not result["verified_recursive_calls"]


def test_super_dispatch_is_not_self_recursion(tmp_path):
    (tmp_path / "fixture.py").write_text(
        "import httpx\nclass Observed(httpx.AsyncHTTPTransport):\n    async def handle_async_request(self, request):\n        return await super().handle_async_request(request)\n"
    )
    original = {
        "nodes": [node("method", "handle_async_request()", line=3)],
        "edges": [edge("method", "method", line=4)],
    }
    fixed, result = audit.normalize(original, tmp_path)
    assert fixed["edges"][0]["target"] == "external_httpx_asynchttptransport_handle_async_request"
    assert result["corrected_calls"][0]["reason"] == "super_dispatch"


def test_real_recursive_call_is_retained(tmp_path):
    (tmp_path / "fixture.py").write_text(
        "def walk(children):\n    return [walk(c) for c in children]\n"
    )
    original = {"nodes": [node("walk", "walk()")], "edges": [edge("walk", "walk", line=2)]}
    fixed, result = audit.normalize(original, tmp_path)
    assert fixed["edges"][0]["source"] == fixed["edges"][0]["target"] == "walk"
    assert fixed["edges"][0]["recursion"] is True
    assert len(result["verified_recursive_calls"]) == 1


def test_parallel_relations_and_locations_survive_normalization(tmp_path):
    (tmp_path / "fixture.py").write_text("pass\n")
    original = {
        "nodes": [node("a", "A"), node("b", "B")],
        "edges": [
            edge("a", "b", "calls", 1),
            edge("a", "b", "uses", 2),
            edge("b", "a", "references", 3),
        ],
    }
    fixed, _ = audit.normalize(original, tmp_path)
    assert fixed["edges"] == original["edges"]
    assert len(fixed["edges"]) == 3


def test_shadowed_import_does_not_create_a_false_function_target(tmp_path):
    (tmp_path / "fixture.py").write_text(
        "from pkg.rules import evaluate\nclass Service:\n    def evaluate(self, evaluate):\n        return evaluate()\n"
    )
    original = {
        "nodes": [
            node("method", ".evaluate()", line=3),
            node("function", "evaluate()", "pkg/rules.py"),
        ],
        "edges": [edge("method", "method", line=4)],
    }
    fixed, result = audit.normalize(original, tmp_path)
    assert fixed["edges"][0]["target"] == "method"
    assert result["unresolved"][0]["reason"] == "Unverified self-call"
    assert not result["corrected_calls"] and not result["verified_recursive_calls"]
