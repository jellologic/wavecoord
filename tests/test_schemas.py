import glob
import json
import os
import unittest

import _paths  # noqa: F401  (sets sys.path)
from wavecoord import jsonschema_lite as js

API_DEFS = {
    "health.json": "health",
    "hb-request.json": "hb_request",
    "hb-response.json": "hb_response",
    "claim-request.json": "claim_request",
    "claim-200.json": "claim_response",
    "claim-409-held.json": "error_body",
    "claim-409-agent-busy.json": "error_body",
    "error-421.json": "error_body",
    "finish-request.json": "finish_request",
    "finish-response.json": "finish_response",
    "task-lease.json": "task_lease",
    "message-request.json": "message_request",
    "inbox.json": "inbox",
    "check-request.json": "check_request",
    "check-response.json": "check_response",
    "reservation-request.json": "reservation_request",
    "notify-request.json": "notify_request",
    "report-request.json": "report_request",
    "tokens-file.json": "tokens_file",
    "mcp-initialize.json": "jsonrpc_exchange",
    "mcp-tools-call-inbox.json": "jsonrpc_exchange",
}
SCHEMA_DIRS = ("task-spec", "report", "event", "config")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def schema(name):
    return load(os.path.join(_paths.SCHEMAS, name + ".schema.json"))


class SchemaFilesTest(unittest.TestCase):
    def test_schemas_are_draft07_and_in_subset(self):
        for p in sorted(glob.glob(os.path.join(_paths.SCHEMAS, "*.schema.json"))):
            s = load(p)
            self.assertEqual(s.get("$schema"), "http://json-schema.org/draft-07/schema#", p)
            js.check_schema(s)  # raises SchemaError on a keyword outside the subset


class FixturesTest(unittest.TestCase):
    def test_valid_fixtures(self):
        n = 0
        for d in SCHEMA_DIRS:
            s = schema(d)
            files = sorted(glob.glob(os.path.join(_paths.FIXTURES, d, "*.json")))
            self.assertTrue(files, "no fixtures for " + d)
            for p in files:
                with self.subTest(fixture=os.path.relpath(p, _paths.FIXTURES)):
                    self.assertEqual(js.validate(load(p), s), [])
                    n += 1
        self.assertGreater(n, 0)

    def test_api_fixtures(self):
        s = schema("api")
        files = sorted(os.listdir(os.path.join(_paths.FIXTURES, "api")))
        self.assertEqual(sorted(API_DEFS), files, "every api fixture needs a definition mapping")
        for fn, d in API_DEFS.items():
            with self.subTest(fixture=fn):
                doc = load(os.path.join(_paths.FIXTURES, "api", fn))
                self.assertEqual(js.validate(doc, s, "#/definitions/" + d), [])

    def test_report_fixtures_in_events_are_valid_reports(self):
        r = schema("report")
        for p in glob.glob(os.path.join(_paths.FIXTURES, "event", "*.json")):
            ev = load(p)
            if ev["kind"] == "report.post":
                self.assertEqual(js.validate(ev["payload"]["report"], r), [])

    def test_invalid_fixtures_fail(self):
        files = sorted(glob.glob(os.path.join(_paths.FIXTURES, "invalid", "*.json")))
        self.assertTrue(files)
        for p in files:
            with self.subTest(fixture=os.path.basename(p)):
                case = load(p)
                errs = js.validate(case["doc"], schema(case["schema"]), case.get("ref"))
                self.assertNotEqual(errs, [], "expected a validation error")


class ValidatorTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(js.validate(1, {"type": "integer"}), [])
        self.assertNotEqual(js.validate(True, {"type": "integer"}), [])
        self.assertNotEqual(js.validate(1.5, {"type": "integer"}), [])
        self.assertEqual(js.validate(1.5, {"type": "number"}), [])
        self.assertEqual(js.validate(None, {"type": ["string", "null"]}), [])

    def test_enum_const_bool_vs_int(self):
        self.assertNotEqual(js.validate(True, {"const": 1}), [])
        self.assertNotEqual(js.validate(1, {"enum": [True]}), [])
        self.assertEqual(js.validate(1.0, {"const": 1}), [])

    def test_object_rules(self):
        s = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}, "additionalProperties": False}
        self.assertEqual(js.validate({"a": "x"}, s), [])
        self.assertNotEqual(js.validate({}, s), [])
        self.assertNotEqual(js.validate({"a": "x", "b": 1}, s), [])
        self.assertNotEqual(js.validate({"a": 1}, s), [])

    def test_arrays(self):
        s = {"type": "array", "items": {"type": "integer"}, "minItems": 1, "uniqueItems": True}
        self.assertEqual(js.validate([1, 2], s), [])
        self.assertNotEqual(js.validate([], s), [])
        self.assertNotEqual(js.validate([1, 1], s), [])
        self.assertNotEqual(js.validate([1, "a"], s), [])

    def test_strings_and_numbers(self):
        self.assertNotEqual(js.validate("ab", {"pattern": "^a$"}), [])
        self.assertNotEqual(js.validate("", {"minLength": 1}), [])
        self.assertNotEqual(js.validate(0, {"exclusiveMinimum": 0}), [])
        self.assertNotEqual(js.validate(11, {"maximum": 10}), [])

    def test_combinators_and_ref(self):
        root = {"definitions": {"s": {"type": "string"}}, "anyOf": [{"$ref": "#/definitions/s"}, {"type": "null"}]}
        self.assertEqual(js.validate(None, root), [])
        self.assertEqual(js.validate("x", root), [])
        self.assertNotEqual(js.validate(1, root), [])
        self.assertNotEqual(js.validate(1, {"oneOf": [{"type": "integer"}, {"type": "number"}]}), [])
        self.assertNotEqual(js.validate(1, {"not": {"type": "integer"}}), [])

    def test_check_schema_walks_nested(self):
        with self.assertRaises(js.SchemaError):
            js.check_schema({"properties": {"a": {"items": {"if": {}}}}})
        js.check_schema({"properties": {"a": {"items": {"type": "string"}}}})

    def test_unknown_keyword_raises(self):
        with self.assertRaises(js.SchemaError):
            js.validate(1, {"if": {"type": "integer"}})
        with self.assertRaises(js.SchemaError):
            js.validate(1, {"$ref": "other.json#/x"})

    def test_glob_rule(self):
        g = schema("task-spec")
        for ok in ("src/**", "a/b.py", "docs/*.md", "..foo/x"):
            self.assertEqual(js.validate(ok, g, "#/definitions/glob"), [], ok)
        for bad in ("/abs", "../x", "a/../b", "a b", "{a,b}", "a/..", ""):
            self.assertNotEqual(js.validate(bad, g, "#/definitions/glob"), [], bad)


if __name__ == "__main__":
    unittest.main()
