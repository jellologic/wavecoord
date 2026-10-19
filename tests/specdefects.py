"""Seeded defects of the spec tree (docs/design/spec-layer.md, section 10). Each is a small patch of the code
under test, and each must make at least one named test fail: a defect that no test catches fails the suite.

"""

import contextlib
import re

from wavecoord.specs import doc, fingerprint, graph, model, ops, status


@contextlib.contextmanager
def patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _flow_render(orig):
    def render(rec):
        text = orig(rec)
        fm, sep, body = text[4:].partition("\n---\n")

        def fold(m):
            items = [l.strip()[2:] for l in m.group(2).split("\n") if l.strip()]
            return "%s: [%s]\n" % (m.group(1), ", ".join(items))
        fm = re.sub(r"^(tags|needs|aliases|paths): *\n((?:  - [^\n]*\n?)+)", fold, fm + "\n", flags=re.M).rstrip("\n")
        return "---\n" + fm + sep + body
    return render


def _status_after_title(orig):
    def render(rec):
        lines = orig(rec).split("\n")
        st = lines.pop(1)  # "status: ..." is the first frontmatter line
        lines.insert(lines.index(next(l for l in lines if l.startswith("title:"))) + 1, st)
        return "\n".join(lines)
    return render


def _rerender_status(text, value):
    rec = doc.parse(text)
    rec["status"] = value
    return doc.render(rec)


def _canonical_with(extra=None, drop=None):
    orig = fingerprint.canonical

    def canonical(rec):
        import json
        data = json.loads(orig(rec).decode("utf-8"))
        if extra:
            data[extra] = rec.get(extra)
        if drop:
            data.pop(drop, None)
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return canonical


def _mv_no_alias(orig):
    def op_mv(w, op):
        orig(w, op)
        for rel in list(w.files):
            if "aliases:" in w.files[rel]:
                r = doc.parse(w.files[rel], ops.kind_of(ops.id_of(rel)))
                r["aliases"] = [a for a in r.get("aliases") or [] if a != op["from"] and not a.startswith(op["from"] + "/")]
                w.files[rel] = doc.render(r)
    return op_mv


def _drop_rule(rule):
    orig = graph.Graph._alignment

    def _alignment(self):
        orig(self)
        self.problems = [p for p in self.problems if p.rule != rule]
    return _alignment


def _fail_open_expand(self, nid):
    """The 0.2.0 expansion: only valid tasks, so an invalid blocker drops out of needs."""
    return [x for x in self.t.subtree(nid) if self.t.nodes[x].kind == "task" and x in self.recs]


@contextlib.contextmanager
def _plan_may_set_done():
    with patch(ops, "lead_transition", lambda old, new: None):
        with patch(ops, "plan_change_problems", lambda before, after: []):
            yield


def _artifact_with_from(a):
    return {k: (fingerprint.norm(v) if isinstance(v, str) else v) for k, v in sorted(a.items())}


DEFECTS = {
    "S01": ("render writes flow lists", lambda: patch(doc, "render", _flow_render(doc.render)),
            ["test_spec_merge.CaseTest.test_p2_sorted_inserts_merge"]),
    "S02": ("inserts append instead of sorting", lambda: patch(model, "SORTED_LISTS", ()),
            ["test_spec_merge.CaseTest.test_p2_sorted_inserts_merge"]),
    "S03": ("the fingerprint includes status", lambda: patch(fingerprint, "canonical", _canonical_with(extra="status")),
            ["test_spec_fp.FingerprintTest.test_unchanged"]),
    "S04": ("the fingerprint ignores outputs", lambda: patch(fingerprint, "canonical", _canonical_with(drop="outputs")),
            ["test_spec_fp.FingerprintTest.test_changed"]),
    "S05": ("mv does not rewrite references", lambda: patch(ops, "_rewrite_refs", lambda *a, **k: None),
            ["test_spec_ops.OpsTest.test_mv_rewrites_references_and_keeps_aliases"]),
    "S06": ("mv does not add an alias", lambda: patch(ops, "op_mv", _mv_no_alias(ops.op_mv)),
            ["test_spec_merge.CaseTest.test_p5_races"]),
    "S07": ("render puts status after title", lambda: patch(doc, "render", _status_after_title(doc.render)),
            ["test_spec_merge.CaseTest.test_p4_status_and_other_keys_merge"]),
    "S08": ("a status commit re-renders the whole file", lambda: patch(status, "set_status_line", _rerender_status),
            ["test_spec_status.UnitTest.test_set_status_line_changes_one_line"]),
    "S09": ("needs on a node does not expand to its subtree",
            lambda: patch(graph.Graph, "expand", lambda self, nid: [nid] if nid in self.tasks else []),
            ["test_spec_graph.RandomTreeTest.test_p8_waves_are_parallel_safe"]),
    "S10": ("check skips A1", lambda: patch(graph.Graph, "_alignment", _drop_rule("A1")),
            ["test_spec_graph.RandomTreeTest.test_p9_injected_misalignment"]),
    "S11": ("waves and P ignore paths overlap", lambda: patch(graph.Graph, "_paths_clash", lambda self, a, b: None),
            ["test_spec_graph.RuleTest.test_rules"]),
    "S12": ("an invalid or unparsable blocker drops out of needs (fail open)",
            lambda: patch(graph.Graph, "expand", _fail_open_expand),
            ["test_spec_graph.FailClosedTest.test_unparsable_or_invalid_blocker_blocks"]),
    "S13": ("a plan change may set a coordinator status", _plan_may_set_done,
            ["test_spec_ops.OpsTest.test_coordinator_status_is_never_a_plan_change",
             "test_spec_ops.OpsTest.test_lead_status_transitions"]),
    "S14": ("the fingerprint includes an input's from", lambda: patch(fingerprint, "_artifact", _artifact_with_from),
            ["test_spec_ops.OpsTest.test_mv_keeps_every_basis"]),
    "S15": ("fmt renders a record under a kind other than its path's",
            lambda: patch(doc, "shape_errors", lambda rec, kind=None: []),
            ["test_spec_doc.NoDataLossTest.test_kind_mismatch_and_unknown_kind"]),
}
