"""``dbctl ask`` — route a natural-language request to a declared operation.

The router NEVER writes SQL: it picks one operation from operations.yaml
and fills its declared parameters, so the worst it can do is run a
whitelisted, dry-run-gated operation. Execution is left to the CLI's
normal safety path (dry-run default, confirm, audit).

Two resolvers:

* **heuristic** (default, offline): token/fuzzy scoring of the question
  against operation names, descriptions and parameters; connection
  inference from mentions; ``key=value`` and positional value extraction.
* **LLM** (optional, ``--llm`` or auto-detected): sends the secret-free
  catalogs plus the question to an Anthropic or OpenAI-compatible chat
  API and parses a JSON plan. Configured via ``DBCTL_LLM_PROVIDER``,
  ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``, ``DBCTL_LLM_MODEL``,
  ``DBCTL_LLM_BASE_URL``, ``DBCTL_LLM_TIMEOUT``. Uses only the stdlib —
  no extra dependency.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dbctl.catalog import connections_catalog, operations_catalog, redact_params

if TYPE_CHECKING:
    from dbctl.config import Connection, Operation


class AskError(Exception):
    """Friendly routing failure — surfaced by the CLI as exit code 2."""


@dataclass
class Plan:
    connection: str | None  # canonical name; None = undetermined (CLI prompts)
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    source: str = "heuristic"  # "heuristic" | "llm" | "llm-fallback"

    def preview(self, ops: dict[str, Operation]) -> str:
        """One-line human summary (secrets masked)."""
        op = ops.get(self.operation)
        masked = redact_params(op, self.params) if op else self.params
        conn = self.connection or "?"
        return f"{conn} :: {self.operation} {masked}".rstrip()


# --------------------------------------------------------------------------- #
# token utilities
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "are",
    "at",
    "between",
    "by",
    "db",
    "database",
    "databases",
    "for",
    "from",
    "in",
    "into",
    "is",
    "me",
    "of",
    "on",
    "or",
    "please",
    "run",
    "show",
    "the",
    "to",
    "top",
    "using",
    "via",
    "with",
}

_KEYVAL_RE = re.compile(r"([A-Za-z_][\w]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,]+)")
_QUOTED_RE = re.compile(r"\"([^\"]*)\"|'([^']*)'")
_VALUE_RE = re.compile(r"\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?|[A-Za-z_][\w-]*")


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
        return v[1:-1]
    return v


# --------------------------------------------------------------------------- #
# heuristic: connection inference
# --------------------------------------------------------------------------- #
def _mention_map(question: str, conns: dict[str, Connection]) -> list[str]:
    """Canonical names of connections mentioned in the question (by name or
    alias, matched on word boundaries so ``pg`` does not also match
    ``pg-ro``), in registry order."""
    qtext = question.lower()
    hits: list[str] = []
    for name, c in conns.items():
        names = [name, *c.aliases]
        if any(re.search(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])", qtext) for n in names):
            hits.append(name)
    return hits


def pick_connection(
    question: str,
    conns: dict[str, Connection],
    *,
    prefer: str | None = None,
) -> str | None:
    """Resolve the target connection or None when ambiguous."""
    from dbctl.connections import resolve

    if prefer is not None:
        try:
            canonical, _ = resolve(prefer, conns)
        except KeyError as e:
            raise AskError(str(e)) from e
        return canonical
    mentioned = _mention_map(question, conns)
    if len(mentioned) == 1:
        return mentioned[0]
    if len(mentioned) > 1:
        raise AskError(
            "request mentions several connections (" + ", ".join(sorted(mentioned)) + "); "
            "pass --conn to pick one"
        )
    if len(conns) == 1:
        return next(iter(conns))
    return None


# --------------------------------------------------------------------------- #
# heuristic: operation scoring
# --------------------------------------------------------------------------- #
def _op_score(question: str, name: str, op: Operation) -> float:
    qtoks = _tokens(question)
    name_tokens = _tokens(name.replace("-", " "))
    desc_tokens = _tokens(op.description or "")
    param_tokens: set[str] = set()
    for p in op.parameters:
        param_tokens |= _tokens(p.name)
        param_tokens |= _tokens(p.description or "")
    score = 3.0 * len(name_tokens & qtoks)
    score += 1.0 * len(desc_tokens & qtoks)
    score += 2.0 * len(param_tokens & qtoks)
    # fuzzy similarity may break ties between token-overlapping candidates,
    # but must never turn zero-overlap noise into a match
    if score > 0:
        score += 2.0 * difflib.SequenceMatcher(None, question.lower(), name.replace("-", " ")).ratio()
    return score


def pick_operation(
    question: str,
    ops: dict[str, Operation],
    *,
    prefer: str | None = None,
) -> str:
    """Score single-scope operations against the question and return the
    best match. Raises AskError when nothing scores above zero. Connection
    whitelists are NOT applied here — ``ensure_allowed`` reports a clear
    error instead of silently rerouting intent to a whitelisted op."""
    from dbctl.operations import resolve as resolve_op

    if prefer is not None:
        if prefer not in ops:
            try:
                resolve_op(prefer, ops)
            except KeyError as e:
                raise AskError(str(e)) from e
        op = ops[prefer]
        if op.scope.value != "single":
            raise AskError(
                f"operation {prefer!r} is multi-scope; `ask` routes single-connection "
                "operations only (run multi ops directly: dbctl <op> ...)"
            )
        return prefer

    candidates = {n: o for n, o in ops.items() if o.scope.value == "single"}
    if not candidates:
        raise AskError("no single-scope operations declared in operations.yaml")

    scored = sorted(
        ((_op_score(question, n, o), n) for n, o in candidates.items()),
        key=lambda t: (-t[0], t[1]),
    )
    best_score, best = scored[0]
    if best_score <= 0:
        sug = difflib.get_close_matches(question.lower(), [n.replace("-", " ") for n in candidates], n=3)
        hint = f" (close matches: {', '.join(sug)})" if sug else ""
        raise AskError(f"no operation matches the request{hint}; pass --op to force one")
    return best


# --------------------------------------------------------------------------- #
# heuristic: parameter extraction
# --------------------------------------------------------------------------- #
def extract_params(
    question: str,
    op_name: str,
    op: Operation,
    *,
    exclude_conn_tokens: set[str],
) -> dict[str, Any]:
    """Best-effort param fill: ``key=value`` pairs first, then positional
    candidates (quoted strings, numbers, bare words) assigned in order to
    the operation's positional parameters. Keyword (non-positional)
    params are only filled via ``key=value``."""
    known = {p.name for p in op.parameters}
    out: dict[str, Any] = {}

    remaining = question
    for m in _KEYVAL_RE.finditer(question):
        key, raw = m.group(1), m.group(2)
        if key in known:
            out[key] = _strip_quotes(raw)
            remaining = remaining.replace(m.group(0), " ", 1)
    if out:
        # re-scan remaining for more occurrences of the same pattern
        for m in _KEYVAL_RE.finditer(remaining):
            key, raw = m.group(1), m.group(2)
            if key in known and key not in out:
                out[key] = _strip_quotes(raw)
                remaining = remaining.replace(m.group(0), " ", 1)

    exclude = _STOPWORDS | exclude_conn_tokens | _tokens(op_name) | _tokens(op.description or "") | known

    candidates: list[str] = []
    for m in _VALUE_RE.finditer(remaining):
        raw = m.group(0)
        val = _strip_quotes(raw)
        if not val or val.lower() in exclude:
            continue
        candidates.append(val)

    positional = sorted((p for p in op.parameters if p.position is not None), key=lambda p: p.position or 0)
    for p, val in zip(positional, candidates, strict=False):
        if p.name not in out:
            out[p.name] = val
    return out


# --------------------------------------------------------------------------- #
# LLM resolver (stdlib-only: Anthropic + OpenAI-compatible chat APIs)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LlmConfig:
    provider: str  # "anthropic" | "openai"
    api_key: str
    model: str
    base_url: str
    timeout: float


def llm_config() -> LlmConfig | None:
    """Detect an LLM provider from the environment; None when unconfigured."""
    provider = (os.environ.get("DBCTL_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
    if provider not in {"anthropic", "openai"}:
        return None
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY") or ""
        model = os.environ.get("DBCTL_LLM_MODEL") or "claude-sonnet-4-5"
        base = os.environ.get("DBCTL_LLM_BASE_URL") or "https://api.anthropic.com"
    else:
        key = os.environ.get("OPENAI_API_KEY") or ""
        model = os.environ.get("DBCTL_LLM_MODEL") or "gpt-4.1"
        base = os.environ.get("DBCTL_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
        base = base or "https://api.openai.com/v1"
    if not key:
        return None
    try:
        timeout = float(os.environ.get("DBCTL_LLM_TIMEOUT") or "30")
    except ValueError:
        timeout = 30.0
    return LlmConfig(provider=provider, api_key=key, model=model, base_url=base.rstrip("/"), timeout=timeout)


_LLM_SYSTEM = """You route natural-language database requests for dbctl, a CLI whose \
operations are pre-declared parameterized SQL blocks. Choose exactly ONE declared \
operation and ONE configured connection; never invent operations, connections, or SQL.

Respond with ONLY a JSON object, no prose, no code fences:
{"connection": "<name or null>", "operation": "<name>", "params": {<declared names only>}, \
"rationale": "<one sentence>"}

Rules:
- params must use only the operation's declared parameter names; omit ones you cannot infer
- respect connection safety: read_only connections cannot run write operations; \
allowed_operations (when non-empty) is a whitelist
- connection may be null only if no configured connection is clearly the target
"""


def _chat(cfg: LlmConfig, system: str, user: str) -> str:
    """One-shot chat completion over urllib. Raises AskError on any failure."""
    if cfg.provider == "anthropic":
        url = f"{cfg.base_url}/v1/messages"
        headers = {
            "x-api-key": cfg.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": cfg.model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    else:
        url = f"{cfg.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.api_key}", "content-type": "application/json"}
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:  # noqa: S310 - fixed https base
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AskError(f"LLM API error {e.code} from {cfg.provider}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AskError(f"LLM request to {cfg.provider} failed: {e}") from e
    try:
        if cfg.provider == "anthropic":
            return str(body["content"][0]["text"])
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise AskError(f"unexpected LLM response shape from {cfg.provider}") from e


def _parse_llm_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise AskError("LLM response contained no JSON object")
    try:
        out = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as e:
        raise AskError(f"LLM response was not valid JSON: {e}") from e
    if not isinstance(out, dict):
        raise AskError("LLM response was not a JSON object")
    return out


def _llm_plan(
    question: str,
    conns: dict[str, Connection],
    ops: dict[str, Operation],
    *,
    prefer_conn: str | None,
    prefer_op: str | None,
) -> Plan:
    cfg = llm_config()
    if cfg is None:
        raise AskError(
            "no LLM configured: set DBCTL_LLM_PROVIDER (anthropic|openai) with "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY (or DBCTL_LLM_BASE_URL for any "
            "OpenAI-compatible endpoint), or omit --llm to use the offline router"
        )
    user_lines = [
        "Connections:",
        json.dumps(connections_catalog(conns), default=str),
        "Operations:",
        json.dumps(operations_catalog(ops), default=str),
    ]
    if prefer_op:
        user_lines.append(f"The operation is fixed: {prefer_op}.")
    if prefer_conn:
        user_lines.append(f"The connection is fixed: {prefer_conn}.")
    user_lines.append(f"Request: {question}")
    raw = _chat(cfg, _LLM_SYSTEM, "\n".join(user_lines))
    data = _parse_llm_json(raw)

    op_name = data.get("operation")
    if not op_name or not isinstance(op_name, str):
        raise AskError("LLM did not choose an operation")
    if prefer_op and op_name != prefer_op:
        op_name = prefer_op  # --op wins over the model's pick
    if op_name not in ops:
        raise AskError(f"LLM chose unknown operation {op_name!r}")
    op = ops[op_name]
    if op.scope.value != "single":
        raise AskError(
            f"LLM chose multi-scope operation {op_name!r}; `ask` routes single-connection operations only"
        )

    conn_name = data.get("connection")
    if conn_name is not None and not isinstance(conn_name, str):
        conn_name = None
    if prefer_conn is not None:
        conn_name = prefer_conn
    if conn_name:
        from dbctl.connections import resolve

        try:
            canonical, _ = resolve(conn_name, conns)
        except KeyError:
            raise AskError(f"LLM chose unknown connection {conn_name!r}") from None
        ensure_allowed(conns[canonical], op_name)
    elif len(conns) == 1:
        canonical = next(iter(conns))
        ensure_allowed(conns[canonical], op_name)
    else:
        canonical = pick_connection(question, conns) or ""
        if canonical:
            ensure_allowed(conns[canonical], op_name)

    declared = {p.name for p in op.parameters}
    params = {k: v for k, v in (data.get("params") or {}).items() if isinstance(k, str) and k in declared}
    rationale = str(data.get("rationale") or "").strip()
    return Plan(
        connection=canonical or None, operation=op_name, params=params, rationale=rationale, source="llm"
    )


# --------------------------------------------------------------------------- #
# safety gate shared with the CLI
# --------------------------------------------------------------------------- #
def ensure_allowed(conn: Connection, op_name: str) -> None:
    if conn.safety.allowed_operations and op_name not in conn.safety.allowed_operations:
        raise AskError(f"operation {op_name!r} is not in the allowed_operations whitelist of that connection")


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def plan_from_question(
    question: str,
    conns: dict[str, Connection],
    ops: dict[str, Operation],
    *,
    prefer_conn: str | None = None,
    prefer_op: str | None = None,
    use_llm: bool | None = None,
) -> Plan:
    """Build a Plan for the question.

    ``use_llm``: None = auto (LLM when configured, heuristic otherwise);
    True = require LLM; False = heuristic only. LLM failures fall back to
    the heuristic unless the LLM was explicitly requested.
    """
    question = question.strip()
    if not question:
        raise AskError("empty request")

    want_llm = use_llm if use_llm is not None else llm_config() is not None
    if want_llm:
        try:
            return _llm_plan(question, conns, ops, prefer_conn=prefer_conn, prefer_op=prefer_op)
        except AskError:
            if use_llm is True:
                raise
        # auto mode: fall through to the heuristic resolver

    conn_name = pick_connection(question, conns, prefer=prefer_conn)
    op_name = pick_operation(question, ops, prefer=prefer_op)
    if conn_name is not None:
        ensure_allowed(conns[conn_name], op_name)

    exclude_conn_tokens: set[str] = set()
    if conn_name is not None:
        c = conns[conn_name]
        exclude_conn_tokens = _tokens(" ".join([conn_name, *c.aliases]))
    params = extract_params(question, op_name, ops[op_name], exclude_conn_tokens=exclude_conn_tokens)
    return Plan(
        connection=conn_name,
        operation=op_name,
        params=params,
        rationale="matched by name/description/parameter overlap",
    )


__all__ = ["AskError", "Plan", "plan_from_question", "ensure_allowed", "llm_config"]
