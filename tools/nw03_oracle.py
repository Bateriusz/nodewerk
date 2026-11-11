#!/usr/bin/env python3
"""Backend-neutral expression parsing, HLSL lowering, and CPU reference oracles.

The parser accepts exactly the expression vocabulary already declared by the
NW-02 ShaderExpr operator schemas.  Lowering preserves the expression DAG's
operator structure; it never introduces per-node function calls or reflection
metadata.  The CPU evaluator is the oracle used to check fused GPU expressions.

Documented numerical tolerance: absolute component error <= 1e-5 for values in
[-1, 1] and relative error <= 1e-4 otherwise when comparing the float32 shader
formulation against the double-precision reference formulation.  The oracle
helper below can quantize each operation to float32 to model GPU arithmetic
more closely.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


NUMERIC_TOLERANCE_ABSOLUTE = 1e-5
NUMERIC_TOLERANCE_RELATIVE = 1e-4

MATH_NOISE_EXPRESSION = (
    "vec4("
    "mix(uv.x, sin(uv.y * 17.0 + time), 0.35), "
    "mix(uv.y, cos(uv.x * 13.0 - time), 0.25), "
    "sin(uv.x * 31.0 + time) * 0.5 + 0.5, "
    "1.0)"
)

_UNSUPPORTED_TEXTURE_OPERATIONS = frozenset({"texture2d", "sample"})
_VECTOR_CTORS = {"vec2": 2, "vec3": 3, "vec4": 4}
_UNARY_FUNCTIONS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "abs": abs,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "frac": lambda value: value - math.floor(value),
    "fract": lambda value: value - math.floor(value),
    "exp": math.exp,
    "log": math.log,
    "round": lambda value: float(round(value)),
    "sign": lambda value: float((value > 0.0) - (value < 0.0)),
    "saturate": lambda value: min(max(value, 0.0), 1.0),
}


class ExpressionError(ValueError):
    """Raised when a ShaderExpr expression cannot be parsed or lowered."""

    def __init__(self, message: str, position: int = 0):
        super().__init__(message)
        self.position = position


@dataclass(frozen=True)
class Literal:
    value: float


@dataclass(frozen=True)
class Variable:
    name: str


@dataclass(frozen=True)
class Call:
    name: str
    arguments: tuple["Expression", ...]


@dataclass(frozen=True)
class Member:
    base: "Expression"
    selector: str


@dataclass(frozen=True)
class Unary:
    operator: str
    operand: "Expression"


@dataclass(frozen=True)
class Binary:
    operator: str
    left: "Expression"
    right: "Expression"


Expression = Literal | Variable | Call | Member | Unary | Binary


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.position = 0

    def parse(self) -> Expression:
        expression = self._additive()
        self._skip_whitespace()
        if self.position != len(self.text):
            raise ExpressionError(
                f"trailing input at byte {self.position}", self.position
            )
        return expression

    def _skip_whitespace(self) -> None:
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1

    def _peek(self) -> str:
        return self.text[self.position] if self.position < len(self.text) else ""

    def _consume(self, character: str) -> bool:
        self._skip_whitespace()
        if self._peek() == character:
            self.position += 1
            return True
        return False

    def _additive(self) -> Expression:
        expression = self._multiplicative()
        while True:
            self._skip_whitespace()
            if self._peek() in {"+", "-"}:
                operator = self._peek()
                self.position += 1
                expression = Binary(operator, expression, self._multiplicative())
            else:
                return expression

    def _multiplicative(self) -> Expression:
        expression = self._unary()
        while True:
            self._skip_whitespace()
            if self._peek() in {"*", "/"}:
                operator = self._peek()
                self.position += 1
                expression = Binary(operator, expression, self._unary())
            else:
                return expression

    def _unary(self) -> Expression:
        self._skip_whitespace()
        if self._peek() in {"+", "-"}:
            operator = self._peek()
            self.position += 1
            return Unary(operator, self._unary())
        return self._postfix()

    def _postfix(self) -> Expression:
        expression = self._primary()
        while True:
            self._skip_whitespace()
            if self._peek() != ".":
                return expression
            self.position += 1
            start = self.position
            while self.position < len(self.text) and (
                self.text[self.position].isalnum() or self.text[self.position] == "_"
            ):
                self.position += 1
            selector = self.text[start : self.position]
            if not selector or len(selector) > 4 or any(
                character not in "xyzwrgbastpq" for character in selector
            ):
                raise ExpressionError(
                    f"unsupported member selector '.{selector}'", start
                )
            expression = Member(expression, selector)

    def _primary(self) -> Expression:
        self._skip_whitespace()
        character = self._peek()
        if not character:
            raise ExpressionError("expression ends unexpectedly", self.position)
        if character.isdigit() or (
            character == "."
            and self.position + 1 < len(self.text)
            and self.text[self.position + 1].isdigit()
        ):
            return self._number()
        if character.isalpha() or character == "_":
            return self._identifier_or_call()
        if character == "(":
            self.position += 1
            expression = self._additive()
            if not self._consume(")"):
                raise ExpressionError("missing ')'", self.position)
            return expression
        raise ExpressionError(
            f"unsupported character {character!r}", self.position
        )

    def _number(self) -> Expression:
        start = self.position
        if self._peek() == ".":
            self.position += 1
        while self.position < len(self.text) and self.text[self.position].isdigit():
            self.position += 1
        if self._peek() == ".":
            self.position += 1
            while (
                self.position < len(self.text)
                and self.text[self.position].isdigit()
            ):
                self.position += 1
        if self._peek() in {"e", "E"}:
            self.position += 1
            if self._peek() in {"+", "-"}:
                self.position += 1
            exponent_start = self.position
            while (
                self.position < len(self.text)
                and self.text[self.position].isdigit()
            ):
                self.position += 1
            if self.position == exponent_start:
                raise ExpressionError("malformed exponent", start)
        end = self.position
        if self._peek() in {"f", "F"}:
            self.position += 1
        try:
            value = float(self.text[start:end])
        except ValueError as exc:
            raise ExpressionError("malformed numeric literal", start) from exc
        if not math.isfinite(value):
            raise ExpressionError("non-finite numeric literal", start)
        return Literal(value)

    def _identifier_or_call(self) -> Expression:
        start = self.position
        while self.position < len(self.text) and (
            self.text[self.position].isalnum() or self.text[self.position] == "_"
        ):
            self.position += 1
        name = self.text[start : self.position]
        self._skip_whitespace()
        if self._peek() != "(":
            if name not in {"uv", "time"}:
                raise ExpressionError(
                    f"bare identifier '{name}' is not a supported value", start
                )
            return Variable(name)
        self.position += 1
        arguments: list[Expression] = []
        self._skip_whitespace()
        if self._peek() == ")":
            self.position += 1
        else:
            while True:
                arguments.append(self._additive())
                self._skip_whitespace()
                if self._peek() == ",":
                    self.position += 1
                    continue
                if self._peek() == ")":
                    self.position += 1
                    break
                raise ExpressionError(
                    f"missing ',' or ')' in call to '{name}'", self.position
                )
        _validate_arity(name, len(arguments))
        return Call(name, tuple(arguments))


def _validate_arity(name: str, count: int) -> None:
    if name in _VECTOR_CTORS:
        expected = _VECTOR_CTORS[name]
        if count != expected:
            raise ExpressionError(f"{name} expects {expected} arguments, got {count}")
        return
    if name in _UNARY_FUNCTIONS or name in {"normalize", "length", "const"}:
        if count != 1:
            raise ExpressionError(f"{name} expects 1 argument, got {count}")
        return
    if name in {"mix", "lerp", "clamp", "smoothstep", "select"}:
        if count != 3:
            raise ExpressionError(f"{name} expects 3 arguments, got {count}")
        return
    if name in {
        "texture2d",
        "sample",
        "pow",
        "min",
        "max",
        "step",
        "dot",
        "cross",
        "add",
        "mul",
    }:
        if count != 2:
            raise ExpressionError(f"{name} expects 2 arguments, got {count}")
        return
    raise ExpressionError(f"unsupported expression operation '{name}'")


def parse_expression(text: str) -> Expression:
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("expression is empty")
    return _Parser(text).parse()


# ---------------------------------------------------------------------------
# HLSL lowering
# ---------------------------------------------------------------------------


def _number_text(value: float) -> str:
    if not math.isfinite(value):
        raise ExpressionError("non-finite literal cannot be lowered")
    if value == 0.0:
        value = 0.0
    text = format(value, ".17g")
    if "." not in text and "e" not in text and "E" not in text:
        text += ".0"
    return text


def _swizzle_text(selector: str) -> str:
    mapping = str.maketrans("rgba", "xyzw")
    mapped = selector.translate(mapping)
    if any(character not in "xyzwstpq" for character in mapped):
        raise ExpressionError(f"unsupported swizzle '{selector}'")
    return mapped


def lower_to_hlsl(expression: Expression) -> str:
    """Lower a parsed expression tree to HLSL expression text."""
    if isinstance(expression, Literal):
        return _number_text(expression.value)
    if isinstance(expression, Variable):
        if expression.name == "uv":
            return "uv"
        if expression.name == "time":
            return "g_time"
        raise ExpressionError(f"unsupported variable '{expression.name}'")
    if isinstance(expression, Member):
        return f"({lower_to_hlsl(expression.base)}).{_swizzle_text(expression.selector)}"
    if isinstance(expression, Unary):
        return f"({expression.operator}{lower_to_hlsl(expression.operand)})"
    if isinstance(expression, Binary):
        return (
            f"({lower_to_hlsl(expression.left)} {expression.operator} "
            f"{lower_to_hlsl(expression.right)})"
        )
    assert isinstance(expression, Call)
    name = expression.name
    if name in _UNSUPPORTED_TEXTURE_OPERATIONS:
        raise ExpressionError(
            f"operation '{name}' requires a bound texture and is not emitted by the "
            "initial compute math/noise pass family"
        )
    hlsl_name = {
        "vec2": "float2",
        "vec3": "float3",
        "vec4": "float4",
        "mix": "lerp",
        "lerp": "lerp",
        "frac": "frac",
        "fract": "frac",
        "saturate": "saturate",
    }.get(name, name)
    arguments = ", ".join(lower_to_hlsl(argument) for argument in expression.arguments)
    return f"{hlsl_name}({arguments})"


def lower_expression_text_to_hlsl(text: str) -> str:
    return lower_to_hlsl(parse_expression(text))


def expression_nodes(expression: Expression) -> tuple[Expression, ...]:
    """Return a deterministic post-order list of unique expression DAG nodes."""
    order: list[Expression] = []
    seen: set[Expression] = set()

    def visit(node: Expression) -> None:
        if node in seen:
            return
        if isinstance(node, Member):
            visit(node.base)
        elif isinstance(node, Unary):
            visit(node.operand)
        elif isinstance(node, Binary):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, Call):
            for argument in node.arguments:
                visit(argument)
        seen.add(node)
        order.append(node)

    visit(expression)
    return tuple(order)


# ---------------------------------------------------------------------------
# CPU reference evaluation
# ---------------------------------------------------------------------------


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _is_scalar(value: float | tuple[float, ...]) -> bool:
    return isinstance(value, (int, float))


def _broadcast(
    left: float | tuple[float, ...],
    right: float | tuple[float, ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if _is_scalar(left) and _is_scalar(right):
        return (float(left),), (float(right),)
    if _is_scalar(left):
        right_tuple = tuple(right)
        return (float(left),) * len(right_tuple), right_tuple
    if _is_scalar(right):
        left_tuple = tuple(left)
        return left_tuple, (float(right),) * len(left_tuple)
    left_tuple = tuple(left)
    right_tuple = tuple(right)
    if len(left_tuple) == len(right_tuple):
        return left_tuple, right_tuple
    if len(left_tuple) == 1:
        return left_tuple * len(right_tuple), right_tuple
    if len(right_tuple) == 1:
        return left_tuple, right_tuple * len(left_tuple)
    raise ExpressionError(
        f"cannot broadcast vectors of width {len(left_tuple)} and {len(right_tuple)}"
    )


def _vector_map(
    value: float | tuple[float, ...],
    function: Any,
) -> float | tuple[float, ...]:
    if _is_scalar(value):
        return float(function(float(value)))
    return tuple(float(function(component)) for component in value)


def _componentwise_binary(
    left: float | tuple[float, ...],
    right: float | tuple[float, ...],
    function: Any,
) -> float | tuple[float, ...]:
    left_tuple, right_tuple = _broadcast(left, right)
    values = tuple(float(function(a, b)) for a, b in zip(left_tuple, right_tuple))
    return values[0] if len(values) == 1 else values


def evaluate(
    expression: Expression,
    *,
    variables: dict[str, Any] | None = None,
    float32: bool = False,
) -> float | tuple[float, ...]:
    """Evaluate a parsed expression against CPU reference variables."""
    variables = variables or {}
    quantize = _float32 if float32 else (lambda value: float(value))

    def visit(node: Expression) -> float | tuple[float, ...]:
        if isinstance(node, Literal):
            return quantize(node.value)
        if isinstance(node, Variable):
            if node.name not in variables:
                raise ExpressionError(f"no value provided for '{node.name}'")
            value = variables[node.name]
            if _is_scalar(value):
                return quantize(float(value))
            return tuple(quantize(component) for component in value)
        if isinstance(node, Member):
            base = visit(node.base)
            if _is_scalar(base):
                base = (float(base),)
            selector = _swizzle_text(node.selector)
            indices = {"x": 0, "y": 1, "z": 2, "w": 3, "s": 0, "t": 1, "p": 2, "q": 3}
            try:
                values = tuple(base[indices[character]] for character in selector)
            except IndexError as exc:
                raise ExpressionError(
                    f"swizzle '.{node.selector}' exceeds vector width {len(base)}"
                ) from exc
            return values[0] if len(values) == 1 else values
        if isinstance(node, Unary):
            operand = visit(node.operand)
            if node.operator == "+":
                return operand
            return _vector_map(operand, lambda value: -value)
        if isinstance(node, Binary):
            left = visit(node.left)
            right = visit(node.right)
            if node.operator == "+":
                return _componentwise_binary(left, right, lambda a, b: a + b)
            if node.operator == "-":
                return _componentwise_binary(left, right, lambda a, b: a - b)
            if node.operator == "*":
                return _componentwise_binary(left, right, lambda a, b: a * b)
            if node.operator == "/":
                return _componentwise_binary(left, right, lambda a, b: a / b)
            raise ExpressionError(f"unsupported binary operator '{node.operator}'")
        assert isinstance(node, Call)
        arguments = [visit(argument) for argument in node.arguments]
        name = node.name
        if name in _VECTOR_CTORS:
            components: list[float] = []
            for argument in arguments:
                if _is_scalar(argument):
                    components.append(float(argument))
                else:
                    components.extend(float(component) for component in argument)
            expected = _VECTOR_CTORS[name]
            if len(components) != expected:
                raise ExpressionError(
                    f"{name} constructed {len(components)} components, expected {expected}"
                )
            return tuple(components)
        if name in _UNSUPPORTED_TEXTURE_OPERATIONS:
            raise ExpressionError(
                f"operation '{name}' has no CPU reference without a texture binding"
            )
        if name in _UNARY_FUNCTIONS:
            return _vector_map(arguments[0], _UNARY_FUNCTIONS[name])
        if name == "normalize":
            value = arguments[0]
            if _is_scalar(value):
                return float(1.0 if value >= 0.0 else -1.0)
            length = math.sqrt(sum(component * component for component in value))
            if length == 0.0:
                return tuple(0.0 for _ in value)
            return tuple(component / length for component in value)
        if name == "length":
            value = arguments[0]
            if _is_scalar(value):
                return abs(float(value))
            return math.sqrt(sum(component * component for component in value))
        if name == "const":
            return arguments[0]
        if name in {"mix", "lerp"}:
            a, b, factor = arguments
            return _mix(a, b, factor)
        if name == "clamp":
            value, low, high = arguments
            return _componentwise_binary(
                _componentwise_binary(value, low, lambda x, lo: max(x, lo)),
                high,
                lambda x, hi: min(x, hi),
            )
        if name == "smoothstep":
            edge0, edge1, value = arguments
            e0_values, e1_values = _broadcast(edge0, edge1)
            x_values, _ = _broadcast(value, e0_values)
            if len(e0_values) == 1 and len(x_values) > 1:
                e0_values = e0_values * len(x_values)
                e1_values = e1_values * len(x_values)
            elif len(x_values) == 1 and len(e0_values) > 1:
                x_values = x_values * len(e0_values)
            t = tuple(
                0.0 if e1 == e0 else min(max((x - e0) / (e1 - e0), 0.0), 1.0)
                for e0, e1, x in zip(e0_values, e1_values, x_values)
            )
            values = tuple(item * item * (3.0 - 2.0 * item) for item in t)
            return values[0] if len(values) == 1 else values
        if name in {"pow", "min", "max", "step", "dot", "cross", "add", "mul"}:
            if name == "pow":
                return _componentwise_binary(arguments[0], arguments[1], math.pow)
            if name == "min":
                return _componentwise_binary(arguments[0], arguments[1], min)
            if name == "max":
                return _componentwise_binary(arguments[0], arguments[1], max)
            if name == "step":
                edge, value = arguments
                return _componentwise_binary(edge, value, lambda e, x: 1.0 if x >= e else 0.0)
            if name == "add":
                return _componentwise_binary(arguments[0], arguments[1], lambda a, b: a + b)
            if name == "mul":
                return _componentwise_binary(arguments[0], arguments[1], lambda a, b: a * b)
            a, b = arguments
            if _is_scalar(a) or _is_scalar(b):
                raise ExpressionError(f"{name} requires vector operands")
            if len(a) != len(b):
                raise ExpressionError(f"{name} operands have different widths")
            if name == "dot":
                return sum(x * y for x, y in zip(a, b))
            if len(a) == 3:
                return (
                    a[1] * b[2] - a[2] * b[1],
                    a[2] * b[0] - a[0] * b[2],
                    a[0] * b[1] - a[1] * b[0],
                )
            raise ExpressionError("cross requires 3-component operands")
        if name == "select":
            condition, when_true, when_false = arguments
            _left, right = _broadcast(when_true, when_false)
            cond, _cond_right = _broadcast(condition, when_true)
            values = tuple(
                truth if truth else false
                for truth, false in zip(cond, right)
            )
            return values[0] if len(values) == 1 else values
        raise ExpressionError(f"unsupported operation '{name}'")

    return visit(expression)


def _mix(
    a: float | tuple[float, ...],
    b: float | tuple[float, ...],
    factor: float | tuple[float, ...],
) -> float | tuple[float, ...]:
    a_broadcast, b_broadcast = _broadcast(a, b)
    f_broadcast, _ = _broadcast(factor, a_broadcast)
    values = tuple(
        av + (bv - av) * fv
        for av, bv, fv in zip(a_broadcast, b_broadcast, f_broadcast)
    )
    return values[0] if len(values) == 1 else values


def evaluate_text(
    text: str,
    *,
    variables: dict[str, Any] | None = None,
    float32: bool = False,
) -> float | tuple[float, ...]:
    return evaluate(parse_expression(text), variables=variables, float32=float32)


_HLSL_REFERENCE_TOKENS = {
    "float2": "vec2",
    "float3": "vec3",
    "float4": "vec4",
    "lerp": "mix",
    "g_time": "time",
}


def hlsl_fused_expression(hlsl_source: str) -> str:
    """Extract the fused ``float4 nw_value = ...;`` expression from generated HLSL."""
    match = re.search(
        r"\bfloat4\s+nw_value\s*=\s*(.*?);", hlsl_source, re.DOTALL
    )
    if match is None:
        raise ExpressionError("generated HLSL has no fused nw_value expression")
    return match.group(1).strip()


def hlsl_expression_to_reference_text(hlsl_source: str) -> str:
    """Translate the emitted HLSL scalar expression into reference-parser syntax."""
    expression = hlsl_fused_expression(hlsl_source)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return _HLSL_REFERENCE_TOKENS.get(token, token)

    return re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\b", replace, expression)


def evaluate_hlsl_source(
    hlsl_source: str,
    uv: Sequence[float],
    time_seconds: float,
    *,
    float32: bool = True,
) -> tuple[float, float, float, float]:
    """Evaluate the generated HLSL fused expression against the same CPU oracle."""
    result = evaluate_text(
        hlsl_expression_to_reference_text(hlsl_source),
        variables={"uv": (uv[0], uv[1]), "time": time_seconds},
        float32=float32,
    )
    assert isinstance(result, tuple) and len(result) == 4
    return tuple(float(component) for component in result)  # type: ignore[return-value]


def math_noise_oracle(
    uv: Sequence[float],
    time_seconds: float,
    *,
    float32: bool = False,
) -> tuple[float, float, float, float]:
    """Reference values for the documented math/noise fixture expression."""
    result = evaluate_text(
        MATH_NOISE_EXPRESSION,
        variables={"uv": (uv[0], uv[1]), "time": time_seconds},
        float32=float32,
    )
    assert isinstance(result, tuple) and len(result) == 4
    return tuple(float(component) for component in result)  # type: ignore[return-value]


def tolerance_failure(
    expected: Sequence[float],
    actual: Sequence[float],
) -> str | None:
    """Return a descriptive failure string when vectors exceed tolerances."""
    if len(expected) != len(actual):
        return f"component count mismatch: expected {len(expected)}, got {len(actual)}"
    for index, (expected_value, actual_value) in enumerate(zip(expected, actual)):
        if not (math.isfinite(expected_value) and math.isfinite(actual_value)):
            if expected_value != actual_value:
                return f"component {index}: non-finite mismatch {expected_value!r} vs {actual_value!r}"
            continue
        difference = abs(expected_value - actual_value)
        scale = max(abs(expected_value), abs(actual_value), 1.0)
        if difference > NUMERIC_TOLERANCE_ABSOLUTE and difference / scale > NUMERIC_TOLERANCE_RELATIVE:
            return (
                f"component {index}: expected {expected_value!r}, got {actual_value!r} "
                f"(abs={difference:.3g})"
            )
    return None


def oracle_report(
    samples: Iterable[tuple[tuple[float, float], float]] | None = None,
    hlsl_expressions: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Build a deterministic oracle report for the math/noise fixture.

    When ``(source_expression, generated_hlsl)`` pairs are supplied, each fused
    ``nw_value`` expression is evaluated in float32 mode and compared against a
    double-precision evaluation of its originating source expression.  This
    exercises the emitted artifact rather than comparing two runs of the same
    source expression.
    """
    if samples is None:
        samples = tuple(
            ((x / 8.0, y / 8.0), index / 16.0)
            for index, (x, y) in enumerate(
                ((x, y) for y in range(4) for x in range(4))
            )
        )
    hlsl_expressions = tuple(hlsl_expressions)
    rows = []
    for uv, time_value in samples:
        reference = math_noise_oracle(uv, time_value)
        float32_values = math_noise_oracle(uv, time_value, float32=True)
        failure = tolerance_failure(reference, float32_values)
        rows.append(
            {
                "uv": [uv[0], uv[1]],
                "time": time_value,
                "reference": list(reference),
                "float32": list(float32_values),
                "tolerance_failure": failure,
            }
        )
    failures = [row for row in rows if row["tolerance_failure"]]
    hlsl_rows: list[dict[str, Any]] = []
    hlsl_failures: list[dict[str, Any]] = []
    for source_index, (source_expression, hlsl_source) in enumerate(
        hlsl_expressions
    ):
        source_parsed = parse_expression(source_expression)
        for row in rows:
            try:
                source_reference = evaluate(
                    source_parsed,
                    variables={
                        "uv": (row["uv"][0], row["uv"][1]),
                        "time": row["time"],
                    },
                    float32=False,
                )
                assert isinstance(source_reference, tuple)
                actual = evaluate_hlsl_source(
                    hlsl_source, row["uv"], row["time"]
                )
                failure = tolerance_failure(source_reference, actual)
            except Exception as exc:
                source_reference = None
                actual = None
                failure = f"HLSL evaluation failed: {exc}"
            hlsl_row = {
                "source_index": source_index,
                "source_expression": source_expression,
                "uv": row["uv"],
                "time": row["time"],
                "reference": list(source_reference)
                if source_reference is not None
                else None,
                "hlsl_float32": list(actual) if actual is not None else None,
                "tolerance_failure": failure,
            }
            hlsl_rows.append(hlsl_row)
            if failure:
                hlsl_failures.append(hlsl_row)
    hlsl_passed = not hlsl_failures
    return {
        "expression": MATH_NOISE_EXPRESSION,
        "sample_count": len(rows),
        "absolute_tolerance": NUMERIC_TOLERANCE_ABSOLUTE,
        "relative_tolerance": NUMERIC_TOLERANCE_RELATIVE,
        "passed": not failures and hlsl_passed,
        "failures": failures,
        "samples": rows,
        "hlsl_expression_evaluation": {
            "source_count": len(hlsl_expressions),
            "sample_count": len(hlsl_rows),
            "passed": hlsl_passed,
            "failures": hlsl_failures,
            "samples": hlsl_rows,
        },
    }
