"""Tokenizer-light ``.d.ts`` parser for TypeScript declaration files.

The PRD (section 6) prefers parsing TypeScript over regex string-
matching, but the Python ecosystem has no maintained TS parser. To stay
within the spirit of the rule while keeping deps minimal, this module
implements a small bracket-depth-aware scanner:

* It tracks balanced ``{ } ( ) < > [ ]`` and string/comment state so it
  never breaks a type in half (regex would mis-split ``(a, b) => c``).
* It preserves preceding JSDoc ``/** ... */`` blocks per member.
* It is explicitly scoped to the shapes ``tsc --emitDeclarationOnly``
  produces: ``export interface <Name>Props { ... }`` bodies.

If we ever hit a d.ts shape it can't parse (e.g. mapped types,
conditional types, or per-property modifiers like ``readonly``), the
right move is to grow this module — not to fall back to regex. The
regex fallback called out by the PRD applies to ``.tsx`` for things
like ``defaultProps`` literals, which Slice 3 doesn't attempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from prism_mcp.entities import Member

# JSDoc tag patterns we currently understand.
_JSDOC_DEFAULT_RE = re.compile(r"@default(?:Value)?\s+(.+?)(?:\n|$)")
_JSDOC_DEPRECATED_RE = re.compile(r"@deprecated\b")


@dataclass(frozen=True)
class ParsedInterface:
    """One ``export interface ... { ... }`` block extracted from a d.ts.

    Args:
        name (str): interface identifier (e.g. ``"ButtonProps"``).
        members (list[Member]): parsed members in source order.
        deprecated (bool): ``True`` if the interface itself carries a
            ``@deprecated`` JSDoc tag.
    """

    name: str
    members: list[Member] = field(default_factory=list)
    deprecated: bool = False


FunctionShape = Literal["function", "arrow-const"]


@dataclass(frozen=True)
class ParsedFunction:
    """One exported callable declaration.

    Covers both shapes ``tsc --emitDeclarationOnly`` produces:

    * ``export declare function name(...): R;``
    * ``export declare const name: (...) => R;``

    Args:
        name (str): exported identifier.
        params (list[Member]): parameters with ``kind="param"``.
        return_type (str): textual return type.
        deprecated (bool): JSDoc ``@deprecated`` on the declaration.
        description (str | None): JSDoc summary, if any.
        shape (FunctionShape): which of the two shapes produced this.
    """

    name: str
    params: list[Member] = field(default_factory=list)
    return_type: str = ""
    deprecated: bool = False
    description: str | None = None
    shape: FunctionShape = "function"


@dataclass(frozen=True)
class ParsedClass:
    """One exported class declaration with its method signatures.

    Args:
        name (str): class identifier.
        methods (list[Member]): methods with ``kind="method"``. The
            method's ``type`` is its full call signature, e.g.
            ``"(key: string) => string"``. Static methods are flagged
            in the description prefix because the v1 ``Member`` model
            doesn't carry a static bit.
        deprecated (bool): JSDoc ``@deprecated`` on the class.
        description (str | None): JSDoc summary, if any.
    """

    name: str
    methods: list[Member] = field(default_factory=list)
    deprecated: bool = False
    description: str | None = None


def parse_interfaces(source: str) -> list[ParsedInterface]:
    """Parse every top-level ``export interface`` block in ``source``.

    Args:
        source (str): full contents of a ``.d.ts`` file.

    Returns:
        list[ParsedInterface]: one entry per ``export interface``
        declaration found at the top level. Nested interfaces (rare in
        component d.ts files) are skipped because the indexer doesn't
        consume them today.
    """
    stripped = _strip_line_comments(source)
    interfaces: list[ParsedInterface] = []

    for header_match, body_text, leading_jsdoc in _iter_interface_blocks(
        stripped
    ):
        name = header_match.group("name")
        members = _parse_member_list(body_text)
        deprecated = bool(
            leading_jsdoc and _JSDOC_DEPRECATED_RE.search(leading_jsdoc)
        )
        interfaces.append(
            ParsedInterface(
                name=name,
                members=members,
                deprecated=deprecated,
            )
        )

    return interfaces


_INTERFACE_HEADER_RE = re.compile(
    r"export\s+interface\s+(?P<name>[A-Za-z_$][\w$]*)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s+extends\s+[^{]+?)?\s*\{",
    re.DOTALL,
)

_FUNCTION_HEADER_RE = re.compile(
    r"export\s+declare\s+function\s+(?P<name>[A-Za-z_$][\w$]*)"
    r"(?:\s*<[^>]*>)?\s*\(",
)

_CONST_ARROW_HEADER_RE = re.compile(
    r"export\s+declare\s+const\s+(?P<name>[A-Za-z_$][\w$]*)\s*:\s*"
    r"(?:<[^>]*>)?\s*\(",
)

_CLASS_HEADER_RE = re.compile(
    r"(?:export\s+)?declare\s+class\s+(?P<name>[A-Za-z_$][\w$]*)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s+extends\s+[^{]+?)?"
    r"(?:\s+implements\s+[^{]+?)?\s*\{",
    re.DOTALL,
)


def _iter_interface_blocks(source: str):
    """Yield ``(header_match, body_text, leading_jsdoc)`` for each interface.

    Walks ``source`` finding ``export interface ... {`` headers and
    extracts the body between matching braces. Skips nested ``{...}``
    correctly.

    Args:
        source (str): line-comment-stripped d.ts content.

    Yields:
        tuple: regex match for the header, raw body text (no outer
        braces), and the JSDoc block that immediately preceded the
        header (or ``""`` if none).
    """
    cursor = 0
    while True:
        match = _INTERFACE_HEADER_RE.search(source, cursor)
        if match is None:
            return
        body_start = match.end()
        body_end = _find_matching_brace(source, body_start)
        if body_end == -1:
            return
        body_text = source[body_start:body_end]
        leading_jsdoc = _preceding_jsdoc(source, match.start())
        yield match, body_text, leading_jsdoc
        cursor = body_end + 1


def _find_matching_brace(source: str, start: int) -> int:
    """Return the index of the ``}`` that matches the implicit ``{``.

    Args:
        source (str): text containing a body that began with ``{``.
            ``start`` is the index *after* that opening brace.
        start (int): index immediately after the opening brace.

    Returns:
        int: index of the matching ``}``, or ``-1`` if unbalanced.
    """
    return _find_matching(source, start, open_char="{", close_char="}")


def _find_matching_paren(source: str, start: int) -> int:
    """Return the index of the ``)`` that matches the implicit ``(``.

    Args:
        source (str): text containing a group that began with ``(``.
        start (int): index immediately after the opening paren.

    Returns:
        int: index of the matching ``)``, or ``-1`` if unbalanced.
    """
    return _find_matching(source, start, open_char="(", close_char=")")


def _find_matching(
    source: str, start: int, open_char: str, close_char: str
) -> int:
    """Generic balanced-delimiter scanner.

    Skips string literals and block comments while counting depth on
    ``open_char`` / ``close_char``. Used by both the brace scanner
    (interface / class bodies) and the paren scanner (function args).

    Args:
        source (str): text being scanned.
        start (int): index immediately after the implicit opener.
        open_char (str): single-character open delimiter (e.g. ``"("``).
        close_char (str): single-character close delimiter
            (e.g. ``")"``).

    Returns:
        int: index of the matching closer, or ``-1`` if unbalanced.
    """
    depth = 1
    i = start
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            close = source.find("*/", i + 2)
            i = n if close == -1 else close + 2
            continue
        if ch in ('"', "'", "`"):
            i = _skip_string(source, i)
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _skip_string(source: str, start: int) -> int:
    """Return the index just after the string literal starting at ``start``.

    Handles single, double, and backtick quotes; respects backslash
    escapes. Backtick template substitutions (``${...}``) are skipped
    naively — we only care that we land past the closing quote.

    Args:
        source (str): full text.
        start (int): index of the opening quote.

    Returns:
        int: index just past the closing quote, or ``len(source)`` if
        the string never closes.
    """
    quote = source[start]
    i = start + 1
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return n


def _preceding_jsdoc(source: str, header_start: int) -> str:
    """Return the JSDoc block ``/** ... */`` immediately preceding the header.

    Args:
        source (str): full text.
        header_start (int): index where ``export interface`` begins.

    Returns:
        str: the JSDoc block including delimiters, or ``""``.
    """
    snippet = source[:header_start].rstrip()
    if not snippet.endswith("*/"):
        return ""
    open_pos = snippet.rfind("/**")
    if open_pos == -1:
        return ""
    return snippet[open_pos : len(snippet)]


def _strip_line_comments(source: str) -> str:
    """Remove ``// ...`` line comments without disturbing strings.

    Block comments stay in place so JSDoc extraction can still find
    them later.

    Args:
        source (str): text to clean.

    Returns:
        str: text with ``//`` comments removed.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            line_end = source.find("\n", i + 2)
            i = n if line_end == -1 else line_end
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            out.append(ch)
            i += 1
            continue
        if ch in ('"', "'", "`"):
            end = _skip_string(source, i)
            out.append(source[i:end])
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_member_list(body: str) -> list[Member]:
    """Split an interface body into :class:`Member` records.

    Members are separated by ``;`` or ``,`` at bracket-depth zero of
    ``{ } ( ) < > [ ]``. JSDoc comments attach to whatever member they
    immediately precede.

    Args:
        body (str): interface body, without the outer braces.

    Returns:
        list[Member]: one entry per declared member.
    """
    members: list[Member] = []
    raw_chunks = _split_top_level(body, separators=";,")

    for chunk in raw_chunks:
        leading_jsdoc, declaration = _peel_jsdoc(chunk)
        declaration = declaration.strip()
        if not declaration:
            continue
        member = _parse_member_declaration(declaration, leading_jsdoc)
        if member is not None:
            members.append(member)

    return members


def _split_top_level(text: str, separators: str) -> list[str]:
    """Split ``text`` on any character in ``separators`` at top depth.

    Top depth means outside any ``{ } ( ) < > [ ]`` group and outside
    any string or block comment.

    The ``=>`` arrow is skipped as a two-character token so the ``>`` in
    a function type's arrow isn't treated as a generic close (which
    would leave depth negative and silently break splitting).

    Args:
        text (str): text to split.
        separators (str): characters that act as top-level separators.

    Returns:
        list[str]: chunks. Empty chunks are preserved so the caller can
        ignore them after stripping.
    """
    chunks: list[str] = []
    start = 0
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            i = _skip_string(text, i)
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = n if close == -1 else close + 2
            continue
        if ch == "=" and i + 1 < n and text[i + 1] == ">":
            i += 2
            continue
        if ch in "{[(<":
            depth += 1
        elif ch in "}])>":
            if depth > 0:
                depth -= 1
        elif depth == 0 and ch in separators:
            chunks.append(text[start:i])
            start = i + 1
        i += 1

    if start < n:
        chunks.append(text[start:])
    return chunks


def _peel_jsdoc(chunk: str) -> tuple[str, str]:
    """Separate a leading JSDoc block from the rest of ``chunk``.

    Args:
        chunk (str): a single member chunk produced by
            :func:`_split_top_level`.

    Returns:
        tuple[str, str]: ``(jsdoc, declaration)``. ``jsdoc`` is empty
        when no leading ``/** ... */`` was found.
    """
    stripped = chunk.lstrip()
    if not stripped.startswith("/**"):
        return "", chunk
    end = stripped.find("*/")
    if end == -1:
        return "", chunk
    jsdoc = stripped[: end + 2]
    declaration = stripped[end + 2 :]
    return jsdoc, declaration


_MEMBER_NAME_RE = re.compile(
    r"""^(?P<name>'[^']+'|"[^"]+"|[A-Za-z_$][\w$]*)\s*(?P<optional>\?)?\s*:\s*(?P<type>.+)$""",
    re.DOTALL,
)


def _parse_member_declaration(declaration: str, jsdoc: str) -> Member | None:
    """Parse a single ``name?: type`` declaration into a :class:`Member`.

    Args:
        declaration (str): the cleaned declaration text.
        jsdoc (str): any JSDoc block that preceded it; may be ``""``.

    Returns:
        Member | None: parsed member, or ``None`` when ``declaration``
        doesn't look like a prop (e.g. an index signature, which we
        skip for v1).
    """
    match = _MEMBER_NAME_RE.match(declaration.strip())
    if match is None:
        return None

    raw_name = match.group("name")
    name = raw_name.strip("'\"")
    optional = match.group("optional") is not None
    type_str = _normalize_whitespace(match.group("type"))

    description = _extract_jsdoc_description(jsdoc)
    default = _extract_jsdoc_default(jsdoc)

    return Member(
        name=name,
        kind="prop",
        type=type_str,
        required=not optional,
        default=default,
        description=description,
    )


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces and trim."""
    return " ".join(text.split()).strip()


def _extract_jsdoc_description(jsdoc: str) -> str | None:
    """Pull the prose paragraph out of a JSDoc block, ignoring tags.

    Args:
        jsdoc (str): raw JSDoc block including ``/**`` and ``*/``.

    Returns:
        str | None: cleaned description, or ``None`` if the block held
        only tags.
    """
    if not jsdoc:
        return None
    inner = jsdoc.strip().removeprefix("/**").removesuffix("*/")
    lines: list[str] = []
    for raw_line in inner.splitlines():
        line = raw_line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        if line.startswith("@"):
            break
        if line:
            lines.append(line)
    text = " ".join(lines).strip()
    return text or None


def _extract_jsdoc_default(jsdoc: str) -> str | None:
    """Return the value of a ``@default`` tag, if present."""
    if not jsdoc:
        return None
    match = _JSDOC_DEFAULT_RE.search(jsdoc)
    if match is None:
        return None
    return match.group(1).strip().strip("`").strip()


def parse_functions(source: str) -> list[ParsedFunction]:
    """Parse top-level function exports from a d.ts file.

    Handles both shapes ``tsc --emitDeclarationOnly`` emits for
    callables:

    * ``export declare function NAME(...): R;`` (classic declaration)
    * ``export declare const NAME: (...) => R;`` (arrow-const, what
      tsc produces for ``export const useFoo = (...) => {...}``)

    Args:
        source (str): full contents of a ``.d.ts`` file.

    Returns:
        list[ParsedFunction]: every top-level callable export, in
        source order. Helpers that aren't exported are ignored.
    """
    stripped = _strip_line_comments(source)
    results: list[ParsedFunction] = []

    cursor = 0
    while True:
        fn_match = _FUNCTION_HEADER_RE.search(stripped, cursor)
        const_match = _CONST_ARROW_HEADER_RE.search(stripped, cursor)
        match = _earliest_match(fn_match, const_match)
        if match is None:
            break

        shape: FunctionShape = (
            "function" if match is fn_match else "arrow-const"
        )
        parsed = _parse_callable_at(stripped, match, shape)
        if parsed is not None:
            results.append(parsed)
        cursor = match.end()

    return results


def parse_classes(source: str) -> list[ParsedClass]:
    """Parse top-level ``declare class`` blocks from a d.ts file.

    Both ``export declare class X { ... }`` and the un-exported
    ``declare class X { ... }`` (which managers usually couple with
    ``export default new X()``) are returned. The caller decides
    whether to keep the un-exported ones; we expose them so a manager
    walker can attach methods to the corresponding ``export default``.

    Args:
        source (str): full contents of a ``.d.ts`` file.

    Returns:
        list[ParsedClass]: classes in source order with their methods.
    """
    stripped = _strip_line_comments(source)
    results: list[ParsedClass] = []

    cursor = 0
    while True:
        match = _CLASS_HEADER_RE.search(stripped, cursor)
        if match is None:
            break
        body_start = match.end()
        body_end = _find_matching_brace(stripped, body_start)
        if body_end == -1:
            break
        body_text = stripped[body_start:body_end]
        leading_jsdoc = _preceding_jsdoc(stripped, match.start())
        methods = _parse_class_members(body_text)

        results.append(
            ParsedClass(
                name=match.group("name"),
                methods=methods,
                deprecated=bool(
                    leading_jsdoc and _JSDOC_DEPRECATED_RE.search(leading_jsdoc)
                ),
                description=_extract_jsdoc_description(leading_jsdoc),
            )
        )
        cursor = body_end + 1

    return results


def _earliest_match(*matches: re.Match[str] | None) -> re.Match[str] | None:
    """Return the leftmost match among the inputs, ignoring ``None``."""
    candidates = [m for m in matches if m is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda m: m.start())


def _parse_callable_at(
    source: str,
    header: re.Match[str],
    shape: FunctionShape,
) -> ParsedFunction | None:
    """Build a :class:`ParsedFunction` from one header match.

    Args:
        source (str): line-comment-stripped d.ts text.
        header (re.Match[str]): a match from ``_FUNCTION_HEADER_RE`` or
            ``_CONST_ARROW_HEADER_RE``; its end position lands one
            character past the opening ``(``.
        shape (FunctionShape): which header pattern matched, used to
            pick the right return-type separator (``):`` vs ``) =>``).

    Returns:
        ParsedFunction | None: the parsed declaration, or ``None`` if
        the parens or return clause are malformed.
    """
    name = header.group("name")
    args_start = header.end()
    args_end = _find_matching_paren(source, args_start)
    if args_end == -1:
        return None

    args_text = source[args_start:args_end]
    params = _parse_param_list(args_text)
    return_type = _extract_return_type(source, args_end + 1, shape)

    leading_jsdoc = _preceding_jsdoc(source, header.start())
    return ParsedFunction(
        name=name,
        params=params,
        return_type=return_type,
        deprecated=bool(
            leading_jsdoc and _JSDOC_DEPRECATED_RE.search(leading_jsdoc)
        ),
        description=_extract_jsdoc_description(leading_jsdoc),
        shape=shape,
    )


def _extract_return_type(
    source: str, after_paren: int, shape: FunctionShape
) -> str:
    """Read everything between the closing ``)`` and the terminating ``;``.

    Args:
        source (str): line-comment-stripped d.ts text.
        after_paren (int): index right after the matched closing
            paren.
        shape (FunctionShape): which header pattern matched.

    Returns:
        str: normalized return type, e.g. ``"void"`` or
            ``"React.RefObject<HTMLDivElement>"``.
    """
    rest = source[after_paren:]
    if shape == "function":
        match = re.match(r"\s*:\s*(.*?);", rest, flags=re.DOTALL)
    else:
        match = re.match(r"\s*=>\s*(.*?);", rest, flags=re.DOTALL)
    if match is None:
        return ""
    return _normalize_whitespace(match.group(1))


def _parse_param_list(args_text: str) -> list[Member]:
    """Split a function/method parameter list into :class:`Member` rows.

    Args:
        args_text (str): text between the function's parens (no outer
            parens).

    Returns:
        list[Member]: parameters with ``kind="param"``.
    """
    members: list[Member] = []
    for chunk in _split_top_level(args_text, separators=","):
        leading_jsdoc, declaration = _peel_jsdoc(chunk)
        declaration = declaration.strip()
        if not declaration:
            continue
        member = _parse_param_declaration(declaration, leading_jsdoc)
        if member is not None:
            members.append(member)
    return members


_PARAM_RE = re.compile(
    r"""^(?P<name>\.\.\.[A-Za-z_$][\w$]*|'[^']+'|"[^"]+"|[A-Za-z_$][\w$]*)"""
    r"""\s*(?P<optional>\?)?\s*:\s*(?P<type>.+?)\s*(?:=\s*(?P<default>.+))?$""",
    re.DOTALL,
)


def _parse_param_declaration(declaration: str, jsdoc: str) -> Member | None:
    """Parse a single ``name?: type [= default]`` parameter."""
    match = _PARAM_RE.match(declaration.strip())
    if match is None:
        return None
    raw_name = match.group("name")
    name = raw_name.strip("'\"")
    optional = match.group("optional") is not None
    type_str = _normalize_whitespace(match.group("type"))
    default_match = match.group("default")
    default = (
        _normalize_whitespace(default_match)
        if default_match is not None
        else None
    )
    return Member(
        name=name,
        kind="param",
        type=type_str,
        required=not optional and default is None,
        default=default,
        description=_extract_jsdoc_description(jsdoc),
    )


_METHOD_HEADER_RE = re.compile(
    r"""^(?:(?P<modifier>static|readonly|protected|private|public)\s+)*"""
    r"""(?P<name>'[^']+'|"[^"]+"|[A-Za-z_$][\w$]*)"""
    r"""\s*(?P<optional>\?)?\s*\(""",
)


def _parse_class_members(body: str) -> list[Member]:
    """Extract method signatures from a class body.

    Fields, constructors, and unparseable lines are skipped — the
    method list is what consumers of a manager actually call.

    Args:
        body (str): class body text without the outer braces.

    Returns:
        list[Member]: methods with ``kind="method"``. The method's
        ``type`` is its full call signature (params + return).
    """
    members: list[Member] = []
    for chunk in _split_top_level(body, separators=";"):
        leading_jsdoc, declaration = _peel_jsdoc(chunk)
        declaration = declaration.strip()
        if not declaration:
            continue
        member = _parse_method_declaration(declaration, leading_jsdoc)
        if member is not None:
            members.append(member)
    return members


def _parse_method_declaration(declaration: str, jsdoc: str) -> Member | None:
    """Parse one class body entry as a method.

    Args:
        declaration (str): one ``;``-separated chunk from the class
            body.
        jsdoc (str): preceding JSDoc block, if any.

    Returns:
        Member | None: method member, or ``None`` for fields,
        constructors, and anything else we don't surface.
    """
    header = _METHOD_HEADER_RE.match(declaration)
    if header is None:
        return None
    name = header.group("name").strip("'\"")
    if name == "constructor":
        return None
    args_start = header.end()
    args_end = _find_matching_paren(declaration, args_start)
    if args_end == -1:
        return None

    args_text = declaration[args_start:args_end]
    return_match = re.match(
        r"\s*:\s*(.*)$", declaration[args_end + 1 :], flags=re.DOTALL
    )
    return_type = (
        _normalize_whitespace(return_match.group(1)) if return_match else "void"
    )

    modifier = header.group("modifier") or ""
    description = _extract_jsdoc_description(jsdoc)
    if modifier == "static":
        description = (
            f"static method. {description}" if description else "static method."
        )

    return Member(
        name=name,
        kind="method",
        type=f"({_normalize_whitespace(args_text)}) => {return_type}",
        required=True,
        description=description,
    )
