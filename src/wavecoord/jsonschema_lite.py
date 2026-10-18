"""A small JSON Schema (draft-07) validator. Stdlib only.

It supports the subset that the wavecoord schemas use. A schema that uses any
other keyword raises SchemaError, so a schema can never pass silently because
a keyword was ignored.

    errors = validate(instance, schema)            # [] when valid
    errors = validate(instance, root, "#/definitions/lease")
"""

import re

ANNOTATIONS = frozenset(
    ["$schema", "$id", "$comment", "title", "description", "default", "examples", "definitions", "format"]
)
KEYWORDS = frozenset(
    [
        "$ref", "type", "enum", "const",
        "properties", "required", "additionalProperties", "propertyNames",
        "minProperties", "maxProperties",
        "items", "minItems", "maxItems", "uniqueItems",
        "minLength", "maxLength", "pattern",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "anyOf", "oneOf", "allOf", "not",
    ]
)


class SchemaError(Exception):
    """The schema itself is invalid or outside the supported subset."""


def _is_type(value, t):
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    raise SchemaError("unknown type %r" % (t,))


def _equal(a, b):
    """JSON equality: 1 == 1.0 but True != 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _resolve(root, ref):
    if not ref.startswith("#"):
        raise SchemaError("only local $ref is supported: %r" % ref)
    node = root
    for part in ref[1:].split("/"):
        if not part:
            continue
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaError("unresolved $ref %r" % ref)
        node = node[part]
    return node


class _Validator(object):
    def __init__(self, root):
        self.root = root
        self._re = {}

    def _regex(self, pattern):
        r = self._re.get(pattern)
        if r is None:
            try:
                r = re.compile(pattern)
            except re.error as e:
                raise SchemaError("bad pattern %r: %s" % (pattern, e))
            self._re[pattern] = r
        return r

    def check(self, inst, schema, path, errs):
        if schema is True:
            return
        if schema is False:
            errs.append("%s: no value is allowed here" % path)
            return
        if not isinstance(schema, dict):
            raise SchemaError("schema at %s is not an object" % path)
        for k in schema:
            if k not in KEYWORDS and k not in ANNOTATIONS:
                raise SchemaError("unsupported keyword %r at %s" % (k, path))

        if "$ref" in schema:
            # draft-07: $ref ignores sibling keywords.
            self.check(inst, _resolve(self.root, schema["$ref"]), path, errs)
            return

        if "type" in schema:
            types = schema["type"]
            types = types if isinstance(types, list) else [types]
            if not any(_is_type(inst, t) for t in types):
                errs.append("%s: expected type %s, got %s" % (path, "|".join(types), _json_type(inst)))
                return
        if "enum" in schema and not any(_equal(inst, v) for v in schema["enum"]):
            errs.append("%s: %r is not one of %r" % (path, inst, schema["enum"]))
        if "const" in schema and not _equal(inst, schema["const"]):
            errs.append("%s: %r is not %r" % (path, inst, schema["const"]))

        if isinstance(inst, str):
            if "minLength" in schema and len(inst) < schema["minLength"]:
                errs.append("%s: shorter than %d" % (path, schema["minLength"]))
            if "maxLength" in schema and len(inst) > schema["maxLength"]:
                errs.append("%s: longer than %d" % (path, schema["maxLength"]))
            if "pattern" in schema and not self._regex(schema["pattern"]).search(inst):
                errs.append("%s: %r does not match %r" % (path, inst, schema["pattern"]))

        if _is_type(inst, "number"):
            if "minimum" in schema and inst < schema["minimum"]:
                errs.append("%s: %r < minimum %r" % (path, inst, schema["minimum"]))
            if "maximum" in schema and inst > schema["maximum"]:
                errs.append("%s: %r > maximum %r" % (path, inst, schema["maximum"]))
            if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
                errs.append("%s: %r <= exclusiveMinimum %r" % (path, inst, schema["exclusiveMinimum"]))
            if "exclusiveMaximum" in schema and inst >= schema["exclusiveMaximum"]:
                errs.append("%s: %r >= exclusiveMaximum %r" % (path, inst, schema["exclusiveMaximum"]))

        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                errs.append("%s: fewer than %d items" % (path, schema["minItems"]))
            if "maxItems" in schema and len(inst) > schema["maxItems"]:
                errs.append("%s: more than %d items" % (path, schema["maxItems"]))
            if schema.get("uniqueItems"):
                for i in range(len(inst)):
                    if any(_equal(inst[i], inst[j]) for j in range(i)):
                        errs.append("%s: item %d is a duplicate" % (path, i))
            if "items" in schema:
                items = schema["items"]
                if isinstance(items, list):
                    raise SchemaError("tuple form of items is not supported at %s" % path)
                for i, v in enumerate(inst):
                    self.check(v, items, "%s[%d]" % (path, i), errs)

        if isinstance(inst, dict):
            if "minProperties" in schema and len(inst) < schema["minProperties"]:
                errs.append("%s: fewer than %d properties" % (path, schema["minProperties"]))
            if "maxProperties" in schema and len(inst) > schema["maxProperties"]:
                errs.append("%s: more than %d properties" % (path, schema["maxProperties"]))
            for k in schema.get("required", []):
                if k not in inst:
                    errs.append("%s: missing required property %r" % (path, k))
            props = schema.get("properties", {})
            for k, v in inst.items():
                sub = "%s.%s" % (path, k)
                if "propertyNames" in schema:
                    self.check(k, schema["propertyNames"], sub + " (name)", errs)
                if k in props:
                    self.check(v, props[k], sub, errs)
                elif "additionalProperties" in schema:
                    ap = schema["additionalProperties"]
                    if ap is False:
                        errs.append("%s: unexpected property %r" % (path, k))
                    else:
                        self.check(v, ap, sub, errs)

        if "allOf" in schema:
            for s in schema["allOf"]:
                self.check(inst, s, path, errs)
        if "anyOf" in schema:
            if not any(not self._errors(inst, s, path) for s in schema["anyOf"]):
                errs.append("%s: matches none of anyOf" % path)
        if "oneOf" in schema:
            n = sum(1 for s in schema["oneOf"] if not self._errors(inst, s, path))
            if n != 1:
                errs.append("%s: matches %d of oneOf, expected exactly 1" % (path, n))
        if "not" in schema and not self._errors(inst, schema["not"], path):
            errs.append("%s: must not match the 'not' schema" % path)

    def _errors(self, inst, schema, path):
        e = []
        self.check(inst, schema, path, e)
        return e


def _json_type(v):
    for t in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _is_type(v, t):
            return t
    return type(v).__name__


def validate(instance, schema, ref=None):
    """Return a list of error strings; empty when `instance` is valid.

    `ref` (for example "#/definitions/lease") selects a sub-schema of `schema`.
    """
    v = _Validator(schema)
    target = _resolve(schema, ref) if ref else schema
    errs = []
    v.check(instance, target, "$", errs)
    return errs


def check_schema(schema, path="#"):
    """Walk every sub-schema and raise SchemaError on a keyword outside the subset."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError("schema at %s is not an object" % path)
    for k, v in schema.items():
        if k not in KEYWORDS and k not in ANNOTATIONS:
            raise SchemaError("unsupported keyword %r at %s" % (k, path))
        if k in ("properties", "definitions"):
            for name, sub in v.items():
                check_schema(sub, "%s/%s/%s" % (path, k, name))
        elif k in ("additionalProperties", "items", "not", "propertyNames"):
            if isinstance(v, list):
                raise SchemaError("tuple form of %s is not supported at %s" % (k, path))
            check_schema(v, "%s/%s" % (path, k))
        elif k in ("anyOf", "oneOf", "allOf"):
            for i, sub in enumerate(v):
                check_schema(sub, "%s/%s/%d" % (path, k, i))
        elif k == "pattern":
            try:
                re.compile(v)
            except re.error as e:
                raise SchemaError("bad pattern at %s: %s" % (path, e))
