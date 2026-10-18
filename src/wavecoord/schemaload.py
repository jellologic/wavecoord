"""Load the JSON schemas, from the zipapp or from the source tree."""

import json
import os
import pkgutil

from wavecoord import jsonschema_lite

_CACHE = {}


def load(name):
    """The parsed schema `name` (for example "event"), cached."""
    if name in _CACHE:
        return _CACHE[name]
    fn = "%s.schema.json" % name
    data = None
    try:
        data = pkgutil.get_data("wavecoord", "_schemas/" + fn)
    except (OSError, ValueError):
        data = None
    if data is None:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "..", "schemas", fn), "rb") as f:
            data = f.read()
    _CACHE[name] = json.loads(data.decode("utf-8"))
    return _CACHE[name]


def errors(instance, name, ref=None):
    """Validation errors of `instance` against schema `name` (and `ref`), [] when valid."""
    schema = load(name)
    if ref:
        return jsonschema_lite.validate(instance, schema, ref)
    return jsonschema_lite.validate(instance, schema)
