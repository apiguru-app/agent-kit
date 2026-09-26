"""Turn a JSON Schema object into a typed Python signature.

The MCP SDK derives a tool's input schema from the Python signature of the
handler, so to drive tools from endpoints.json we synthesize functions whose
annotations round-trip back to the schema we started with. Patterns, enums,
numeric bounds, descriptions and defaults all survive the trip.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any, Callable, Literal, Optional

from pydantic import Field

_PRIMITIVES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}

# JSON Schema keyword -> pydantic Field keyword
_CONSTRAINTS = {
    "minimum": "ge",
    "maximum": "le",
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
}


def _annotation_for(prop: dict[str, Any]) -> Any:
    """Base type annotation for one JSON Schema property."""
    if "enum" in prop:
        return Literal[tuple(prop["enum"])]  # type: ignore[valid-type]
    return _PRIMITIVES.get(prop.get("type", "string"), str)


def _field_for(prop: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    if prop.get("description"):
        kwargs["description"] = prop["description"]
    for json_key, field_key in _CONSTRAINTS.items():
        if json_key in prop:
            kwargs[field_key] = prop[json_key]
    return Field(**kwargs)


def build_parameters(input_schema: dict[str, Any]) -> list[inspect.Parameter]:
    """JSON Schema object -> ordered keyword-only parameters.

    Required properties come first so the synthesized signature is legal
    (parameters without defaults cannot follow ones with defaults).
    """
    required = set(input_schema.get("required", []))
    props: dict[str, Any] = input_schema.get("properties", {})

    ordered = sorted(props.items(), key=lambda kv: kv[0] not in required)

    params: list[inspect.Parameter] = []
    for name, prop in ordered:
        base = _annotation_for(prop)

        if name in required:
            annotation = Annotated[base, _field_for(prop)]
            default: Any = inspect.Parameter.empty
        elif "default" in prop:
            # A schema default means the server already has a sensible value;
            # keep the type narrow and hand the default to the model.
            annotation = Annotated[base, _field_for(prop)]
            default = prop["default"]
        else:
            # Genuinely optional with no server-side default: the model must
            # be able to leave it out entirely.
            annotation = Annotated[Optional[base], _field_for(prop)]
            default = None

        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )
    return params


def typed_function(
    name: str,
    doc: str,
    input_schema: dict[str, Any],
    impl: Callable[..., Any],
    return_model: Any = None,
) -> Callable[..., Any]:
    """Build an async function with `input_schema`'s signature.

    `impl` receives the validated arguments as a single dict. When
    `return_model` is given it becomes the return annotation, which is what
    the SDK derives the tool's `outputSchema` from.
    """
    params = build_parameters(input_schema)

    async def handler(**kwargs: Any) -> Any:
        return await impl(kwargs)

    handler.__name__ = name
    handler.__doc__ = doc
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        params,
        return_annotation=return_model if return_model is not None else inspect.Signature.empty,
    )
    handler.__annotations__ = {p.name: p.annotation for p in params}
    if return_model is not None:
        handler.__annotations__["return"] = return_model
    return handler
