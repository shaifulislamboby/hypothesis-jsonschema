"""
Canonicalisation logic for JSON schemas.

The canonical format that we transform to is not intended for human consumption.
Instead, it prioritises locality of reasoning - for example, we convert oneOf
arrays into an anyOf of allOf (each sub-schema being the original plus not anyOf
the rest).  Resolving references and merging subschemas is also really helpful.

All this effort is justified by the huge performance improvements that we get
when converting to Hypothesis strategies.  To the extent possible there is only
one way to generate any given value... but much more importantly, we can do
most things by construction instead of by filtering.  That's the difference
between "I'd like it to be faster" and "doesn't finish at all".
"""
import itertools
import json
import math
import re
from copy import deepcopy
from typing import Any, Dict, List, NoReturn, Optional, Tuple, Union
from urllib.parse import urljoin

import jsonschema
from hypothesis.errors import InvalidArgument
from hypothesis.internal.floats import next_down as ieee_next_down, next_up

from ._encode import JSONType, encode_canonical_json, sort_key

Schema = Dict[str, JSONType]
JSONSchemaValidator = Union[
    jsonschema.validators.Draft4Validator,
    jsonschema.validators.Draft6Validator,
    jsonschema.validators.Draft7Validator,
]

# Canonical type strings, in order.
TYPE_STRINGS = ("null", "boolean", "integer", "number", "string", "array", "object")
TYPE_SPECIFIC_KEYS = (
    ("number", "multipleOf maximum exclusiveMaximum minimum exclusiveMinimum"),
    ("integer", "multipleOf maximum exclusiveMaximum minimum exclusiveMinimum"),
    ("string", "maxLength minLength pattern format contentEncoding contentMediaType"),
    ("array", "items additionalItems maxItems minItems uniqueItems contains"),
    (
        "object",
        "maxProperties minProperties required properties patternProperties "
        "additionalProperties dependencies propertyNames",
    ),
)
# Names of keywords where the associated values may be schemas or lists of schemas.
SCHEMA_KEYS = tuple(
    "items additionalItems contains additionalProperties propertyNames "
    "if then else allOf anyOf oneOf not".split()
)
# Names of keywords where the value is an object whose values are schemas.
# Note that in some cases ("dependencies"), the value may be a list of strings.
SCHEMA_OBJECT_KEYS = ("properties", "patternProperties", "dependencies")
ALL_KEYWORDS = tuple(
    [*SCHEMA_KEYS, *SCHEMA_OBJECT_KEYS]
    + sum((s.split() for _, s in TYPE_SPECIFIC_KEYS), [])
)


def next_down(val: float) -> float:
    """Compensate for JSONschema's lack of negative zero with an extra step."""
    out = ieee_next_down(val)
    if out == 0 and math.copysign(1, out) == -1:
        out = ieee_next_down(out)
    assert isinstance(out, float)
    return out


def _get_validator_class(schema: Schema) -> JSONSchemaValidator:
    try:
        validator = jsonschema.validators.validator_for(schema)
        validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError:
        validator = jsonschema.Draft4Validator
        validator.check_schema(schema)
    return validator


class LocalResolver(jsonschema.RefResolver):
    def resolve_remote(self, uri: str) -> NoReturn:
        raise HypothesisRefResolutionError(
            f"hypothesis-jsonschema does not fetch remote references (uri={uri!r})"
        )


def make_validator(
    schema: Schema, resolver: LocalResolver = None
) -> JSONSchemaValidator:
    if resolver is None:
        resolver = LocalResolver.from_schema(schema)
    validator = _get_validator_class(schema)
    return validator(schema, resolver=resolver)


class HypothesisRefResolutionError(jsonschema.exceptions.RefResolutionError):
    pass


def get_type(schema: Schema) -> List[str]:
    """Return a canonical value for the "type" key.

    Note that this will return [], the empty list, if the value is a list without
    any allowed type names; *even though* this is explicitly an invalid value.
    """
    type_ = schema.get("type", list(TYPE_STRINGS))
    # Canonicalise the "type" key to a sorted list of type strings.
    if isinstance(type_, str):
        assert type_ in TYPE_STRINGS
        return [type_]
    assert isinstance(type_, list) and set(type_).issubset(TYPE_STRINGS), type_
    type_ = [t for t in TYPE_STRINGS if t in type_]
    if "number" in type_ and "integer" in type_:
        type_.remove("integer")  # all integers are numbers, so this is redundant
    return type_


def upper_bound_instances(schema: Schema) -> float:
    """Return an upper bound on the number of instances that match this schema."""
    if schema == FALSEY:
        return 0
    if "const" in schema:
        return 1
    if "enum" in schema:
        assert isinstance(schema["enum"], list)
        return len(schema["enum"])
    if get_type(schema) == ["integer"]:
        lower, upper = get_integer_bounds(schema)
        if lower is not None and upper is not None:
            mul = schema.get("multipleOf")
            if isinstance(mul, int):
                return 1 + (upper - lower) % mul
            return 1 + (upper - lower)  # Non-integer mul can only reduce upper bound
    if (
        get_type(schema) == ["array"]
        and isinstance(schema.get("items"), dict)
        and schema.get("maxItems", math.inf) < 100  # type: ignore
    ):
        # For simplicity, we use the upper bound with replacement; while we could
        # tighten this by considering uniqueItems it's not worth the extra code.
        items_bound = upper_bound_instances(schema["items"])  # type: ignore
        if items_bound < 100:
            lo, hi = schema.get("minItems", 0), schema["maxItems"]
            assert isinstance(lo, int) and isinstance(hi, int)
            return sum(items_bound ** n for n in range(lo, hi + 1))
    return math.inf


def _get_numeric_bounds(
    schema: Schema,
) -> Tuple[Optional[float], Optional[float], bool, bool]:
    """Get the min and max allowed numbers, and whether they are exclusive."""
    lower = schema.get("minimum")
    upper = schema.get("maximum")
    exmin = schema.get("exclusiveMinimum", False)
    exmax = schema.get("exclusiveMaximum", False)
    assert lower is None or isinstance(lower, (int, float))
    assert upper is None or isinstance(upper, (int, float))
    assert isinstance(exmin, (bool, int, float))
    assert isinstance(exmax, (bool, int, float))

    # Canonicalise to number-and-boolean representation
    if exmin is not True and exmin is not False:
        if lower is None or exmin >= lower:
            lower, exmin = exmin, True
        else:
            exmin = False
    if exmax is not True and exmax is not False:
        if upper is None or exmax <= upper:
            upper, exmax = exmax, True
        else:
            exmax = False
    assert isinstance(exmin, bool)
    assert isinstance(exmax, bool)
    return lower, upper, exmin, exmax


def get_number_bounds(
    schema: Schema,
) -> Tuple[Optional[float], Optional[float], bool, bool]:
    """Get the min and max allowed floats, and whether they are exclusive."""
    lower, upper, exmin, exmax = _get_numeric_bounds(schema)
    if lower is not None:
        lo = float(lower)
        if lo < lower:
            lo = next_up(lo)
            exmin = False
        lower = lo
    if upper is not None:
        hi = float(upper)
        if hi > upper:
            hi = next_down(hi)
            exmax = False
        upper = hi
    return lower, upper, exmin, exmax


def get_integer_bounds(schema: Schema) -> Tuple[Optional[int], Optional[int]]:
    """Get the min and max allowed integers."""
    lower, upper, exmin, exmax = _get_numeric_bounds(schema)
    # Adjust bounds and cast to int
    if lower is not None:
        lo = math.ceil(lower)
        if exmin and lo == lower:
            lo += 1
        lower = lo
    if upper is not None:
        hi = math.floor(upper)
        if exmax and hi == upper:
            hi -= 1
        upper = hi
    return lower, upper


def canonicalish(schema: JSONType, resolver: LocalResolver = None) -> Dict[str, Any]:
    """Convert a schema into a more-canonical form.

    This is obviously incomplete, but improves best-effort recognition of
    equivalent schemas and makes conversion logic simpler.
    """
    if schema is True:
        return {}
    elif schema is False:
        return {"not": {}}

    # Make a copy, so we don't mutate the existing schema in place.
    # Using the canonical encoding makes all integer-valued floats into ints.
    schema = json.loads(encode_canonical_json(schema))

    # Otherwise, we're dealing with "objects", i.e. dicts.
    if not isinstance(schema, dict):
        raise InvalidArgument(
            f"Got schema={schema!r} of type {type(schema).__name__}, "
            "but expected a dict."
        )

    if resolver is None:
        resolver = LocalResolver.from_schema(schema)

    if "const" in schema:
        if not make_validator(schema, resolver=resolver).is_valid(schema["const"]):
            return FALSEY
        return {"const": schema["const"]}
    if "enum" in schema:
        validator = make_validator(schema, resolver=resolver)
        enum_ = sorted(
            (v for v in schema["enum"] if validator.is_valid(v)), key=sort_key
        )
        if not enum_:
            return FALSEY
        elif len(enum_) == 1:
            return {"const": enum_[0]}
        return {"enum": enum_}
    # if/then/else schemas are ignored unless if and another are present
    if_ = schema.pop("if", None)
    then = schema.pop("then", schema)
    else_ = schema.pop("else", schema)
    if if_ is not None and (then is not schema or else_ is not schema):
        if then not in (if_, TRUTHY) or else_ != TRUTHY:
            alternatives = [
                {"allOf": [if_, then, schema]},
                {"allOf": [{"not": if_}, else_, schema]},
            ]
            schema = canonicalish({"anyOf": alternatives})
    assert isinstance(schema, dict)
    # Recurse into the value of each keyword with a schema (or list of them) as a value
    for key in SCHEMA_KEYS:
        if isinstance(schema.get(key), list):
            schema[key] = [canonicalish(v, resolver=resolver) for v in schema[key]]
        elif isinstance(schema.get(key), (bool, dict)):
            schema[key] = canonicalish(schema[key], resolver=resolver)
        else:
            assert key not in schema, (key, schema[key])
    for key in SCHEMA_OBJECT_KEYS:
        if key in schema:
            schema[key] = {
                k: v if isinstance(v, list) else canonicalish(v, resolver=resolver)
                for k, v in schema[key].items()
            }

    type_ = get_type(schema)
    if "number" in type_:
        if schema.get("exclusiveMinimum") is False:
            del schema["exclusiveMinimum"]
        if schema.get("exclusiveMaximum") is False:
            del schema["exclusiveMaximum"]
        lo, hi, exmin, exmax = get_number_bounds(schema)
        mul = schema.get("multipleOf")
        if isinstance(mul, int):
            # Numbers which are a multiple of an integer?  That's the integer type.
            type_.remove("number")
            type_ = [t for t in TYPE_STRINGS if t in type_ or t == "integer"]
        elif lo is not None and hi is not None:
            lobound = next_up(lo) if exmin else lo
            hibound = next_down(hi) if exmax else hi
            if (
                mul and not has_divisibles(lo, hi, mul, exmin, exmax)
            ) or lobound > hibound:
                type_.remove("number")
            elif type_ == ["number"] and lobound == hibound:
                return {"const": lobound}

    if "integer" in type_:
        lo, hi = get_integer_bounds(schema)
        mul = schema.get("multipleOf")
        if lo is not None and isinstance(mul, int) and mul > 1 and (lo % mul):
            lo += mul - (lo % mul)
        if hi is not None and isinstance(mul, int) and mul > 1 and (hi % mul):
            hi -= hi % mul

        if lo is not None:
            schema["minimum"] = lo
            schema.pop("exclusiveMinimum", None)
        if hi is not None:
            schema["maximum"] = hi
            schema.pop("exclusiveMaximum", None)

        if lo is not None and hi is not None and lo > hi:
            type_.remove("integer")

    if "array" in type_ and "contains" in schema:
        if isinstance(schema.get("items"), dict):
            contains_items = merged(
                [schema["contains"], schema["items"]], resolver=resolver
            )
            if contains_items is not None:
                schema["contains"] = contains_items

        if schema["contains"] == FALSEY:
            type_.remove("array")
        else:
            schema["minItems"] = max(schema.get("minItems", 0), 1)
        if schema["contains"] == TRUTHY:
            schema.pop("contains")
            schema["minItems"] = max(schema.get("minItems", 1), 1)
    if (
        "array" in type_
        and "uniqueItems" in schema
        and isinstance(schema.get("items", []), dict)
    ):
        item_count = upper_bound_instances(schema["items"])
        if math.isfinite(item_count):
            schema["maxItems"] = min(item_count, schema.get("maxItems", math.inf))
    if "array" in type_ and schema.get("minItems", 0) > schema.get(
        "maxItems", math.inf
    ):
        type_.remove("array")
    if (
        "array" in type_
        and "minItems" in schema
        and isinstance(schema.get("items", []), dict)
    ):
        count = upper_bound_instances(schema["items"])
        if (count == 0 and schema["minItems"] > 0) or (
            schema.get("uniqueItems", False) and count < schema["minItems"]
        ):
            type_.remove("array")
    if "array" in type_ and isinstance(schema.get("items"), list):
        schema["items"] = schema["items"][: schema.get("maxItems")]
        for idx, s in enumerate(schema["items"]):
            if s == FALSEY:
                schema["items"] = schema["items"][:idx]
                schema["maxItems"] = idx
                schema.pop("additionalItems", None)
                break
        if schema.get("minItems", 0) > min(
            len(schema["items"])
            + upper_bound_instances(schema.get("additionalItems", TRUTHY)),
            schema.get("maxItems", math.inf),
        ):
            type_.remove("array")
    if (
        "array" in type_
        and isinstance(schema.get("items"), list)
        and schema.get("additionalItems") == FALSEY
    ):
        schema.pop("maxItems", None)
    if "array" in type_ and (
        schema.get("items") == FALSEY or schema.get("maxItems", 1) == 0
    ):
        schema["maxItems"] = 0
        schema.pop("items", None)
        schema.pop("uniqueItems", None)
        schema.pop("additionalItems", None)
    if "array" in type_ and schema.get("items", TRUTHY) == TRUTHY:
        schema.pop("items", None)
    if (
        "properties" in schema
        and not schema.get("patternProperties")
        and schema.get("additionalProperties") == FALSEY
    ):
        max_props = schema.get("maxProperties", math.inf)
        assert isinstance(max_props, (int, float))
        schema["maxProperties"] = min(max_props, len(schema["properties"]))
    if "object" in type_ and schema.get("minProperties", 0) > schema.get(
        "maxProperties", math.inf
    ):
        type_.remove("object")
    # Discard dependencies values that don't restrict anything
    for k, v in schema.get("dependencies", {}).copy().items():
        if v == [] or v == TRUTHY:
            schema["dependencies"].pop(k)
    # Remove no-op keywords
    for kw, identity in {
        "minItems": 0,
        "items": {},
        "additionalItems": {},
        "dependencies": {},
        "minProperties": 0,
        "properties": {},
        "propertyNames": {},
        "patternProperties": {},
        "additionalProperties": {},
        "required": [],
    }.items():
        if kw in schema and schema[kw] == identity:
            schema.pop(kw)
    # Canonicalise "required" schemas to remove redundancy
    if "object" in type_ and "required" in schema:
        assert isinstance(schema["required"], list)
        reqs = set(schema["required"])
        if schema.get("dependencies"):
            # When the presence of a required property requires other properties via
            # dependencies, those properties can be moved to the base required keys.
            dep_names = {
                k: sorted(set(v))
                for k, v in schema["dependencies"].items()
                if isinstance(v, list)
            }
            schema["dependencies"].update(dep_names)
            while reqs.intersection(dep_names):
                for r in reqs.intersection(dep_names):
                    reqs.update(dep_names.pop(r))
                    schema["dependencies"].pop(r)
                    # TODO: else merge schema-dependencies of required properties
                    # into the base schema after adding required back in and being
                    # careful to avoid an infinite loop...
            if not schema["dependencies"]:
                schema.pop("dependencies")
        schema["required"] = sorted(reqs)
        max_ = schema.get("maxProperties", float("inf"))
        assert isinstance(max_, (int, float))
        properties = schema.get("properties", {})
        if len(schema["required"]) > max_:
            type_.remove("object")
        elif any(properties.get(name, {}) == FALSEY for name in schema["required"]):
            type_.remove("object")
        else:
            propnames = schema.get("propertyNames", {})
            validator = make_validator(propnames)
            if not all(validator.is_valid(name) for name in schema["required"]):
                type_.remove("object")

    for t, kw in TYPE_SPECIFIC_KEYS:
        numeric = {"number", "integer"}
        if t in type_ or (t in numeric and numeric.intersection(type_)):
            continue
        for k in kw.split():
            schema.pop(k, None)

    # Canonicalise "not" subschemas
    if "not" in schema:
        not_ = schema.pop("not")

        negated = []
        to_negate = not_["anyOf"] if set(not_) == {"anyOf"} else [not_]
        for not_ in to_negate:
            type_keys = {k: set(v.split()) for k, v in TYPE_SPECIFIC_KEYS}
            type_constraints = {"type"}
            for v in type_keys.values():
                type_constraints |= v
            if set(not_).issubset(type_constraints):
                not_["type"] = get_type(not_)
                for t in set(type_).intersection(not_["type"]):
                    if not type_keys.get(t, set()).intersection(not_):
                        type_.remove(t)
                        if t not in ("integer", "number"):
                            not_["type"].remove(t)
                not_ = canonicalish(not_, resolver=resolver)

            m = merged([not_, {**schema, "type": type_}], resolver=resolver)
            if m is not None:
                not_ = m
            if not_ != FALSEY:
                negated.append(not_)
        if len(negated) > 1:
            schema["not"] = {"anyOf": negated}
        elif negated:
            schema["not"] = negated[0]

    assert isinstance(type_, list), type_
    if not type_:
        assert type_ == []
        return FALSEY
    if type_ == ["null"]:
        return {"const": None}
    if type_ == ["boolean"]:
        return {"enum": [False, True]}
    if type_ == ["null", "boolean"]:
        return {"enum": [None, False, True]}
    if len(type_) == 1:
        schema["type"] = type_[0]
    elif type_ == get_type({}):
        schema.pop("type", None)
    else:
        schema["type"] = type_
    # Canonicalise "xxxOf" lists; in each case canonicalising and sorting the
    # sub-schemas then handling any key-specific logic.
    if TRUTHY in schema.get("anyOf", ()):
        schema.pop("anyOf", None)
    if "anyOf" in schema:
        i = 0
        while i < len(schema["anyOf"]):
            s = schema["anyOf"][i]
            if set(s) == {"anyOf"}:
                schema["anyOf"][i : i + 1] = s["anyOf"]
                continue
            i += 1
        schema["anyOf"] = [
            json.loads(s)
            for s in sorted(
                {encode_canonical_json(a) for a in schema["anyOf"] if a != FALSEY}
            )
        ]
        if not schema["anyOf"]:
            return FALSEY
        if len(schema) == len(schema["anyOf"]) == 1:
            return schema["anyOf"][0]  # type: ignore
        types = []
        # Turn
        #   {"anyOf": [{"type": "string"}, {"type": "null"}]}
        # into
        #   {"type": ["string", "null"]}
        for subschema in schema["anyOf"]:
            if "type" in subschema and len(subschema) == 1:
                types.extend(get_type(subschema))
            else:
                break
        else:
            # All subschemas have only the "type" keyword, then we merge all types
            # into the parent schema
            del schema["anyOf"]
            new_types = canonicalish({"type": types})
            schema = merged([schema, new_types])
            assert isinstance(schema, dict)  # merging was certainly valid
    if "allOf" in schema:
        schema["allOf"] = [
            json.loads(enc)
            for enc in sorted(set(map(encode_canonical_json, schema["allOf"])))
        ]
        if any(s == FALSEY for s in schema["allOf"]):
            return FALSEY
        if all(s == TRUTHY for s in schema["allOf"]):
            schema.pop("allOf")
        elif len(schema) == len(schema["allOf"]) == 1:
            return schema["allOf"][0]  # type: ignore
        else:
            tmp = schema.copy()
            ao = tmp.pop("allOf")
            out = merged([tmp] + ao, resolver=resolver)
            if isinstance(out, dict):  # pragma: no branch
                schema = out
                # TODO: this assertion is soley because mypy 0.750 doesn't know
                # that `schema` is a dict otherwise. Needs minimal report upstream.
                assert isinstance(schema, dict)
    if "oneOf" in schema:
        one_of = schema.pop("oneOf")
        assert isinstance(one_of, list)
        one_of = sorted(one_of, key=encode_canonical_json)
        one_of = [s for s in one_of if s != FALSEY]
        if len(one_of) == 1:
            m = merged([schema, one_of[0]], resolver=resolver)
            if m is not None:  # pragma: no branch
                return m
        if (not one_of) or one_of.count(TRUTHY) > 1:
            return FALSEY
        schema["oneOf"] = one_of
    if schema.get("uniqueItems") is False:
        del schema["uniqueItems"]
    return schema


TRUTHY = canonicalish(True)
FALSEY = canonicalish(False)


def is_recursive_reference(reference: str, resolver: LocalResolver) -> bool:
    """Detect if the given reference is recursive."""
    # Special case: a reference to the schema's root is always recursive
    if reference == "#":
        return True
    # During reference resolving the scope might go to external schemas. `hypothesis-jsonschema` does not support
    # schemas behind remote references, but the underlying `jsonschema` library includes meta schemas for
    # different JSON Schema drafts that are available transparently, and they count as external schemas in this context.
    # For this reason we need to check the reference relatively to the base uri.
    full_reference = urljoin(resolver.base_uri, reference)
    # If a fully-qualified reference is in the resolution stack, then we encounter it for the second time.
    # Therefore it is a recursive reference.
    return full_reference in resolver._scopes_stack


def resolve_all_refs(
    schema: Union[bool, Schema], *, resolver: LocalResolver
) -> Tuple[Schema, bool]:
    """Resolve all non-recursive references in the given schema.

    When a recursive reference is detected, it stops traversing the currently resolving branch and leaves it as is.
    """
    if isinstance(schema, bool):
        return canonicalish(schema), False
    assert isinstance(schema, dict), schema
    if not isinstance(resolver, jsonschema.RefResolver):
        raise InvalidArgument(
            f"resolver={resolver} (type {type(resolver).__name__}) is not a RefResolver"
        )

    if "$ref" in schema:
        # Recursive references are skipped to avoid infinite recursion.
        if not is_recursive_reference(schema["$ref"], resolver):
            s = dict(schema)
            ref = s.pop("$ref")
            with resolver.resolving(ref) as got:
                if s == {}:
                    return resolve_all_refs(deepcopy(got), resolver=resolver)
                m = merged([s, got], resolver=resolver)
                if m is None:  # pragma: no cover
                    msg = f"$ref:{ref!r} had incompatible base schema {s!r}"
                    raise HypothesisRefResolutionError(msg)
                # `deepcopy` is not needed, because, the schemas are copied inside the `merged` call above
                return resolve_all_refs(m, resolver=resolver)
        else:
            return schema, True

    for key in SCHEMA_KEYS:
        val = schema.get(key, False)
        if isinstance(val, list):
            value = []
            for v in val:
                if isinstance(v, dict):
                    resolved, is_recursive = resolve_all_refs(
                        deepcopy(v), resolver=resolver
                    )
                    if is_recursive:
                        return schema, True
                    else:
                        value.append(resolved)
                else:
                    value.append(v)
            schema[key] = value
        elif isinstance(val, dict):
            resolved, is_recursive = resolve_all_refs(deepcopy(val), resolver=resolver)
            if is_recursive:
                return schema, True
            else:
                schema[key] = resolved
        else:
            assert isinstance(val, bool)
    for key in SCHEMA_OBJECT_KEYS:  # values are keys-to-schema-dicts, not schemas
        if key in schema:
            subschema = schema[key]
            assert isinstance(subschema, dict)
            value = {}
            for k, v in subschema.items():
                if isinstance(v, dict):
                    resolved, is_recursive = resolve_all_refs(
                        deepcopy(v), resolver=resolver
                    )
                    if is_recursive:
                        return schema, True
                    else:
                        value[k] = resolved
                else:
                    value[k] = v
            schema[key] = value
    assert isinstance(schema, dict)
    return schema, False


def merged(schemas: List[Any], resolver: LocalResolver = None) -> Optional[Schema]:
    """Merge *n* schemas into a single schema, or None if result is invalid.

    Takes the logical intersection, so any object that validates against the returned
    schema must also validate against all of the input schemas.

    None is returned for keys that cannot be merged short of pushing parts of
    the schema into an allOf construct, such as the "contains" key for arrays -
    there is no other way to merge two schema that could otherwise be applied to
    different array elements.
    It's currently also used for keys that could be merged but aren't yet.
    """
    assert schemas, "internal error: must pass at least one schema to merge"
    schemas = sorted(
        (canonicalish(s, resolver=resolver) for s in schemas), key=upper_bound_instances
    )
    if any(s == FALSEY for s in schemas):
        return FALSEY
    out = schemas[0]
    for s in schemas[1:]:
        if s == TRUTHY:
            continue
        # If we have a const or enum, this is fairly easy by filtering:
        if "const" in out:
            if make_validator(s, resolver=resolver).is_valid(out["const"]):
                continue
            return FALSEY
        if "enum" in out:
            validator = make_validator(s, resolver=resolver)
            enum_ = [v for v in out["enum"] if validator.is_valid(v)]
            if not enum_:
                return FALSEY
            elif len(enum_) == 1:
                out = {"const": enum_[0]}
            else:
                out = {"enum": enum_}
            continue

        if "type" in out and "type" in s:
            tt = s.pop("type")
            ot = get_type(out)
            if "number" in ot:
                ot.append("integer")
            out["type"] = [
                t for t in ot if t in tt or t == "integer" and "number" in tt
            ]
            out_type = get_type(out)
            if not out_type:
                return FALSEY
            for t, kw in TYPE_SPECIFIC_KEYS:
                numeric = ["number", "integer"]
                if t in out_type or t in numeric and t in out_type + numeric:
                    continue
                for k in kw.split():
                    s.pop(k, None)
                    out.pop(k, None)

        # OK, this is a tricky bit, because we have three overlapping parts.
        # First we'll deal with the `properties` keyword, containing schemas for
        # the values associated with an exact key - we merge this with the exact
        # match from the other schema *or* all of the matching patternProperties
        # *or* the additionalProperties schema if there are no matches, in that
        # order.
        out_add = out.get("additionalProperties", {})
        s_add = s.pop("additionalProperties", {})
        out_pat = out.get("patternProperties", {})
        s_pat = s.pop("patternProperties", {})
        if "properties" in out or "properties" in s:
            # The get/pop/setdefault dance and if-statements ensure that we end up with
            # none of these keys present in `s`, and avoid adding them to `out` which
            # can cause an infinite loop of recursive merging.
            out_props = out.setdefault("properties", {})
            s_props = s.pop("properties", {})
            for prop_name in set(out_props) | set(s_props):
                if prop_name in out_props:
                    out_combined = out_props[prop_name]
                else:
                    out_combined = merged(
                        [s for p, s in out_pat.items() if re.search(p, prop_name)]
                        or [out_add],
                        resolver=resolver,
                    )
                if prop_name in s_props:
                    s_combined = s_props[prop_name]
                else:
                    s_combined = merged(
                        [s for p, s in s_pat.items() if re.search(p, prop_name)]
                        or [s_add],
                        resolver=resolver,
                    )
                if out_combined is None or s_combined is None:  # pragma: no cover
                    # Note that this can only be the case if we were actually going to
                    # use the schema which we attempted to merge, i.e. prop_name was
                    # not in the schema and there were unmergable pattern schemas.
                    return None
                m = merged([out_combined, s_combined], resolver=resolver)
                if m is None:
                    return None
                out_props[prop_name] = m
        # With all the property names done, it's time to handle the patterns.  This is
        # simpler as we merge with either an identical pattern, or additionalProperties.
        if out_pat or s_pat:
            for pattern in set(out_pat) | set(s_pat):
                m = merged(
                    [out_pat.get(pattern, out_add), s_pat.get(pattern, s_add)],
                    resolver=resolver,
                )
                if m is None:  # pragma: no cover
                    return None
                out_pat[pattern] = m
            out["patternProperties"] = out_pat
        # Finally, we merge togther the additionalProperties schemas.
        if out_add or s_add:
            m = merged([out_add, s_add], resolver=resolver)
            if m is None:  # pragma: no cover
                return None
            out["additionalProperties"] = m

        if "allOf" in out and "allOf" in s:
            # All our allOf schemas will be de-duplicated by canonicalise
            out["allOf"] += s.pop("allOf")
        if "required" in out and "required" in s:
            out["required"] = sorted(set(out["required"] + s.pop("required")))
        for key in (
            {"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"}
            & set(s)
            & set(out)
        ):
            out[key] = min([out[key], s.pop(key)])
        for key in (
            {"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties"}
            & set(s)
            & set(out)
        ):
            out[key] = max([out[key], s.pop(key)])
        if "multipleOf" in out and "multipleOf" in s:
            x, y = s.pop("multipleOf"), out["multipleOf"]
            if isinstance(x, int) and isinstance(y, int):
                out["multipleOf"] = x * y // math.gcd(x, y)
            elif x != y:
                ratio = max(x, y) / min(x, y)
                if ratio == int(ratio):  # e.g. x=0.5, y=2
                    out["multipleOf"] = max(x, y)
                else:
                    return None
        if "contains" in out and "contains" in s and out["contains"] != s["contains"]:
            # If one `contains` schema is a subset of the other, we can discard it.
            m = merged([out["contains"], s["contains"]], resolver=resolver)
            if m == out["contains"] or m == s["contains"]:
                out["contains"] = m
                s.pop("contains")
        if "not" in out and "not" in s and out["not"] != s["not"]:
            out["not"] = {"anyOf": [out["not"], s.pop("not")]}
        if (
            "dependencies" in out
            and "dependencies" in s
            and out["dependencies"] != s["dependencies"]
        ):
            # Note: draft 2019-09 added separate keywords for name-dependencies
            # and schema-dependencies, but when we add support for that it will
            # be by canonicalising to the existing backwards-compatible keyword.
            #
            # In each dependencies dict, the keys are property names and the values
            # are either a list of required names, or a schema that the whole
            # instance must match.  To merge a list and a schema, convert the
            # former into a `required` key!
            odeps = out["dependencies"]
            for k, v in odeps.copy().items():
                if k in s["dependencies"]:
                    sval = s["dependencies"].pop(k)
                    if isinstance(v, list) and isinstance(sval, list):
                        odeps[k] = v + sval
                        continue
                    if isinstance(v, list):
                        v = {"required": v}
                    elif isinstance(sval, list):
                        sval = {"required": sval}
                    m = merged([v, sval], resolver=resolver)
                    if m is None:
                        return None
                    odeps[k] = m
            odeps.update(s.pop("dependencies"))
        if "items" in out or "items" in s:
            oitems = out.pop("items", TRUTHY)
            sitems = s.pop("items", TRUTHY)
            if isinstance(oitems, list) and isinstance(sitems, list):
                out["items"] = []
                out["additionalItems"] = merged(
                    [
                        out.get("additionalItems", TRUTHY),
                        s.get("additionalItems", TRUTHY),
                    ],
                    resolver=resolver,
                )
                for a, b in itertools.zip_longest(oitems, sitems):
                    if a is None:
                        a = out.get("additionalItems", TRUTHY)
                    elif b is None:
                        b = s.get("additionalItems", TRUTHY)
                    out["items"].append(merged([a, b], resolver=resolver))
            elif isinstance(oitems, list):
                out["items"] = [merged([x, sitems], resolver=resolver) for x in oitems]
                out["additionalItems"] = merged(
                    [out.get("additionalItems", TRUTHY), sitems], resolver=resolver
                )
            elif isinstance(sitems, list):
                out["items"] = [merged([x, oitems], resolver=resolver) for x in sitems]
                out["additionalItems"] = merged(
                    [s.get("additionalItems", TRUTHY), oitems], resolver=resolver
                )
            else:
                out["items"] = merged([oitems, sitems], resolver=resolver)
                if out["items"] is None:
                    return None
            if isinstance(out["items"], list) and None in out["items"]:
                return None
            if out.get("additionalItems", TRUTHY) is None:
                return None
            s.pop("additionalItems", None)

        # This loop handles the remaining cases.  Notably, we do not attempt to
        # merge distinct values for:
        # - `pattern`; computing regex intersection is out of scope
        # - `contains`; requires allOf and thus enters an infinite loop
        # - `$ref`; if not already resolved we can't do that here
        # - `anyOf`; due to product-like explosion in worst case
        # - `oneOf`; which we plan to handle as an anyOf-not composition
        # - `if`/`then`/`else`; which is removed by canonicalisation
        for k, v in s.items():
            if k not in out:
                out[k] = v
            elif out[k] != v and k in ALL_KEYWORDS:
                # If non-validation keys like `title` or `description` don't match,
                # that doesn't really matter and we'll just go with first we saw.
                return None
        out = canonicalish(out, resolver=resolver)
        if out == FALSEY:
            return FALSEY
    assert isinstance(out, dict)
    _get_validator_class(out)
    return out


def has_divisibles(
    start: float, end: float, divisor: float, exmin: bool, exmax: bool
) -> bool:
    """If the given range from `start` to `end` has any numbers divisible by `divisor`."""
    divisible_num = end // divisor - start // divisor
    if not exmin and not start % divisor:
        divisible_num += 1
    if exmax and not end % divisor:
        divisible_num -= 1
    return divisible_num >= 1
