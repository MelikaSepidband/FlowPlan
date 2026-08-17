"""ReprServer: the uniform FastMCP wrapper enforcing the repr-study contract.

Every repr-study server constructs a ReprServer and registers 3-5 plain
implementation functions with ``@server.tool(description=...)``. The wrapper
enforces the uniform contract from INTERFACES.md:

- result envelope {items, truncated, next_page_token, index_state}
- 4,000-token output budget (servers.common.budget)
- staleness disclosure via index_state (servers.common.staleness)
- exceptions converted to the error envelope
- one JSON line per call appended to $RUN_DIR/mcp_calls.jsonl
- the navigation kit (find_symbol) auto-registered unless include_kit=False;
  list_files is opt-in via include_list_files (off by default -- see __init__)
"""

import functools
import inspect
import json
import os
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from mcp.server.fastmcp import FastMCP

from servers.common import budget, env, navkit, paging, staleness

__all__ = ["FIND_SYMBOL_DESCRIPTION", "LIST_FILES_DESCRIPTION", "ReprServer"]

# Frozen kit descriptions -- byte-identical across every server/condition.
FIND_SYMBOL_DESCRIPTION = (
    "Find symbols by name in the prebuilt symbol index. Performs a "
    "case-insensitive substring match of pattern against symbol names and "
    "qualified names; kind filters to function, class, or module (function "
    "includes methods), default any. Returns name, kind, file, line, and "
    "signature anchors, never code bodies. Example: "
    "find_symbol(pattern='run_pipeline', kind='function')."
)
LIST_FILES_DESCRIPTION = (
    "List repository files matching a glob pattern. Returns repo-relative "
    "paths only; the directories .git, .venv, .repr_index, __pycache__, and "
    "node_modules are always excluded. Example: list_files(glob='*.py')."
)


class ReprServer:
    """A FastMCP stdio server enforcing the uniform repr-study tool contract."""

    def __init__(
        self,
        name: str,
        include_kit: bool = False,
        include_list_files: bool = False,
        compact: bool | None = None,
    ) -> None:
        """Create the server; register the navigation kit only when asked.

        DESIGN (2026-06-14): representation servers register ONLY their
        structural tools -- no navigation kit -- so any measured effect is
        attributable purely to the representation. ``include_kit`` (default
        False) registers ``find_symbol`` and ``include_list_files`` (default
        False) registers ``list_files``; BOTH are OFF for the representation
        servers and ON only for the dedicated kit-only ``nav`` control
        condition.

        Rationale: a trajectory audit showed ``find_symbol`` (a semantic but
        still name-lookup tool, i.e. "grep done well") was used as a terminal
        answer -- the agent resolved a symbol, read its returned file path, and
        answered the file-localization task WITHOUT ever invoking the
        representation (73-96% of deps/cfg/dfg MCP runs used only the kit).
        That confounds "representation vs raw search" with "better symbol
        search". Removing the kit forces the structural tools to carry the
        condition; the agent may still use the built-in Read/Bash to discover
        an exact symbol NAME (shared overhead across all conditions, including
        baseline), but the analysis must go through the representation.

        COMPACT (2026-06-15, the ``_compact`` conditions): when ``compact`` is
        True the result envelope is item-compacted (drop empties, alias keys,
        truncate free text) and emitted as a no-whitespace JSON STRING so
        FastMCP does not re-pretty-print it. ``compact=None`` (the default)
        reads the ``REPR_COMPACT`` env var (truthy -> on) at construction so a
        single server module can be launched in either mode from its MCP
        config. The token lever measured on real envelopes is ~34-43% off the
        MCP-result tokens; existing free/strict conditions launch with compact
        off and stay byte-identical.
        """
        self.name = name
        self._mcp = FastMCP(name)
        self._tools: dict[str, Callable[..., dict]] = {}
        if compact is None:
            compact = os.environ.get("REPR_COMPACT", "").strip().lower() in (
                "1", "true", "yes", "on",
            )
        self._compact = compact
        # INLINE-SOURCE mode (2026-06-15): when REPR_INLINE=1, a drill-down
        # tool's result items get the enclosing-definition SOURCE attached
        # inline (via source_tool.inline_source_for), so when the agent queries
        # a SPECIFIC part it sees the actual code WITHOUT a separate Read --
        # killing the Read=8-12 fallback the anchor-only repx run showed. The
        # <rep>_explore entry-point is EXEMPT (it stays a pure representation
        # view). Existing conditions never set the var -> byte-identical.
        self._inline = os.environ.get("REPR_INLINE", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        # KIT OVERRIDE (2026-06-15): a representation server's create_server()
        # passes include_kit=False (the kit is normally only on the nav
        # control), but REPR_INCLUDE_KIT=1 in the launch env forces the kit ON
        # without editing any create_server() body. Used by the
        # ``<rep>_compact_nav_*`` conditions to test whether re-adding
        # find_symbol/list_files recovers the recall the kit-less compact
        # conditions lost. Existing conditions never set the var, so they stay
        # byte-identical.
        if os.environ.get("REPR_INCLUDE_KIT", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            include_kit = True
            include_list_files = True
        if include_kit:
            self.tool(description=FIND_SYMBOL_DESCRIPTION)(navkit.find_symbol)
        if include_list_files:
            self.tool(description=LIST_FILES_DESCRIPTION)(navkit.list_files)
        explore_on = os.environ.get("REPR_EXPLORE", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        # Lever B (substitution): in compact mode every representation server
        # also exposes get_source, so the agent reads the relevant definition
        # body THROUGH the rep (rep tool -> anchor -> get_source) instead of
        # calling the built-in Read on raw source (~10k tokens/run on
        # LinuxFLBench). Registered only in compact conditions so existing
        # free/strict servers stay byte-identical.
        #
        # get_source stays available EVEN when explore is on: a measured v2 run
        # that REMOVED it backfired -- the agent just fell back to the built-in
        # Read (0.4 -> 8.4/run) and tokens went UP. The lever is explore being
        # COMPLETE enough that the agent doesn't need to fetch more, not taking
        # fetch tools away; get_source is a cheap fallback for the rare symbol
        # explore didn't cover.
        if compact:
            from servers.common import source_tool

            self.tool(description=source_tool.GET_SOURCE_DESCRIPTION)(
                source_tool.get_source
            )
        # Fused EXPLORE tool (codegraph-style): REPR_EXPLORE=1 registers a
        # single tool that returns the relevant symbols' verbatim source
        # grouped by file PLUS a relationships note -- collapsing the
        # find -> anchor -> get_source loop into ONE call (the measured reason
        # codegraph cut LinuxFLBench tokens ~48% vs our reps' ~20%). Gated on
        # its own env var so it is a cleanly separable study factor; the
        # ``<rep>_explore_*`` conditions set it. Existing conditions never set
        # it, so they stay byte-identical. explore aggregates over symbols.db /
        # docs.db / callgraph.db, which the ast (+ kit) launch path can read.
        if explore_on:
            from servers.common import explore as explore_mod

            self.tool(description=explore_mod.EXPLORE_DESCRIPTION)(
                explore_mod.explore
            )

    def maybe_add_rep_explore(
        self, impl: "Callable[..., Any]", description: str
    ) -> None:
        """Register a per-representation ``<rep>_explore`` entry-point tool.

        ONLY when REPR_REPEXPLORE=1 (the representation-comparison conditions).
        Unlike the generic shared ``explore`` (same raw-source tool on every
        server), each server passes its OWN view renderer here (ast -> skeleton,
        callgraph -> call subgraph, ...), so the tool is unique per
        representation. Existing conditions never set the var -> byte-identical.
        The agent uses it as the FLOW entry point, then navigates with this
        server's other tools.
        """
        if os.environ.get("REPR_REPEXPLORE", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            return
        # CGIDX: replace this rep's explore with the codegraph-index-backed
        # explore (codegraph's unified graph + ported RWR engine). The point of
        # the _cgidx_ experiment is that every rep's explore is codegraph-quality;
        # the rep name is preserved (<rep>_explore) for per-rep telemetry.
        if os.environ.get("REPR_CGIDX", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            from servers.common import rep_explore
            cg_impl, cg_desc = rep_explore.cgidx_explore_for(self.name)
            self.tool(description=cg_desc)(cg_impl)
            # codegraph_node read-replacement: <rep>_get_source over the index,
            # so the agent reads code without the built-in Read (the missing
            # half of codegraph's explore+node design).
            gs_impl, gs_desc = rep_explore.cgidx_get_source_for(self.name)
            self.tool(description=gs_desc)(gs_impl)
            return
        self.tool(description=description)(impl)

    def maybe_add_cg_tools(
        self, rep: str, extra: "list[tuple[Callable[..., Any], str]] | None" = None
    ) -> None:
        """Register codegraph-faithful tools when REPR_CG=1 (the ``_cg_`` conds).

        The codegraph method (replicated per representation) adds, on top of the
        rep's own tools + fat ``<rep>_explore``: a ``<rep>_get_source`` read-
        replacement (codegraph_node's symbol/anchor mode -- one shared impl,
        rep-named for telemetry) and any rep-specific ``extra`` tools (e.g.
        callgraph's transitive ``impact``). Gated on its own env var so existing
        conditions never see these tools and stay byte-identical.
        """
        if os.environ.get("REPR_CG", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            return
        from servers.common import source_tool

        impl, desc = source_tool.make_get_source_alias(rep)
        self.tool(description=desc)(impl)
        for extra_impl, extra_desc in (extra or []):
            self.tool(description=extra_desc)(extra_impl)

    @property
    def mcp(self) -> FastMCP:
        """The underlying FastMCP instance (e.g. for list_tools/call_tool)."""
        return self._mcp

    @property
    def tools(self) -> Mapping[str, Callable[..., dict]]:
        """Read-only mapping of registered tool name to envelope wrapper."""
        return MappingProxyType(self._tools)

    def tool(
        self, description: str, name: str | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., dict]]:
        """Decorator registering an implementation function as an MCP tool.

        The implementation is a synchronous function returning list[dict]
        items (already limited by its own max_results parameter) and raising
        exceptions for error cases. The returned wrapper produces the full
        result envelope.
        """

        def decorator(impl: Callable[..., Any]) -> Callable[..., dict]:
            tool_name = name or impl.__name__
            wrapper = self._wrap(impl, tool_name)
            self._tools[tool_name] = wrapper
            self._mcp.tool(
                name=tool_name, description=description, structured_output=False
            )(wrapper)
            return wrapper

        return decorator

    def call(self, tool_name: str, **kwargs: Any) -> dict:
        """Invoke a registered tool in-process; returns the envelope dict."""
        try:
            wrapper = self._tools[tool_name]
        except KeyError:
            raise KeyError(
                f"unknown tool {tool_name!r} on server {self.name!r}; "
                f"registered: {sorted(self._tools)}"
            ) from None
        return wrapper(**kwargs)

    def serve(self) -> None:
        """Run the server over the stdio transport (blocking)."""
        self._mcp.run(transport="stdio")

    # -- internals ---------------------------------------------------------

    def _wrap(self, impl: Callable[..., Any], tool_name: str) -> Callable[..., dict]:
        """Build the envelope-enforcing wrapper around an implementation."""

        @functools.wraps(impl)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            state = self._index_state()
            items: list = []
            truncated = False
            error: dict | None = None
            try:
                raw = impl(*args, **kwargs)
                # A tool may declare a higher per-call output budget via a
                # ``_repr_budget`` attribute (e.g. explore, which fuses many
                # symbols' source into one report and is deliberately larger
                # than the default 4k -- codegraph's equivalent returns ~5.5k
                # tokens). Default to the study-wide TOKEN_BUDGET.
                call_budget = getattr(impl, "_repr_budget", budget.TOKEN_BUDGET)
                raw_items = list(raw) if raw is not None else []
                # INLINE-SOURCE: attach the enclosing-def code to the top
                # drill-down results (NOT <rep>_explore -- the view stays pure),
                # so the agent sees the actual code of the specific part it
                # queried without a separate Read. Done BEFORE budget.fit so the
                # fitter accounts for the added source and trims accordingly.
                if self._inline and not tool_name.endswith("_explore"):
                    raw_items = self._attach_inline_source(raw_items)
                items, truncated = budget.fit(raw_items, budget=call_budget)
            except Exception as exc:
                items, truncated = [], False
                error = {"type": type(exc).__name__, "message": str(exc)}
            envelope: dict = {
                "items": items,
                "truncated": truncated,
                "next_page_token": paging.encode_token(len(items)) if truncated else None,
                "index_state": state,
            }
            if error is not None:
                envelope["error"] = error
            # Log the canonical (uncompacted) envelope so mcp_calls.jsonl is
            # identical across compact/non-compact conditions -- only the WIRE
            # format the agent sees differs.
            self._log_call(tool_name, args, kwargs, envelope, start)
            if self._compact:
                return self._compact_wire(envelope)
            return envelope

        # Expose the impl's parameter schema but erase the return type, so
        # FastMCP schema generation sees clean parameters. The wrapper returns
        # either the envelope dict (normal mode) or a pre-serialized compact
        # JSON string (compact mode); with structured_output=False FastMCP does
        # not schema-validate the return, so leaving it untyped is correct.
        wrapper.__signature__ = inspect.signature(impl).replace(  # type: ignore[attr-defined]
            return_annotation=inspect.Signature.empty
        )
        wrapper.__annotations__ = dict(getattr(impl, "__annotations__", {}))
        wrapper.__annotations__.pop("return", None)
        return wrapper

    #: How many of a tool's result items get inline source attached. Bounds a
    #: wide drill-down (e.g. callers_of with 30 callers) so the call stays in
    #: budget; results are in relevance order so the top few are the useful ones.
    _INLINE_MAX_ITEMS = 8

    def _attach_inline_source(self, items: list) -> list:
        """Attach ``inline_source`` (enclosing-def code) to top DISTINCT items.

        Each distinct definition (keyed by its resolved span, so the many
        call-site rows for one callee collapse to one) gets an ``inline_source``
        dict {start_line, end_line, code} so the agent reads the specific part's
        code inline instead of calling Read. The key is ``inline_source`` (NOT
        ``source``) because some tools already use a ``source`` field for the
        query anchor. Duplicates reuse the one fetched body (no repeated
        bodies). Only the first ``_INLINE_MAX_ITEMS`` DISTINCT definitions are
        enriched (relevance order); the rest keep their anchors. Never raises.
        """
        from servers.common import source_tool

        cache: dict = {}            # (file, span) -> inline source dict
        enriched = 0
        for item in items:
            if not isinstance(item, dict) or "inline_source" in item:
                continue
            file = item.get("file")
            line = item.get("line")
            if not isinstance(line, int):
                line = item.get("site_line")
            if not file or not isinstance(line, int):
                continue
            try:
                src = source_tool.inline_source_for(file, line)
            except Exception:
                src = None
            if src is None:
                continue
            key = (file, src["start_line"], src["end_line"])
            if key in cache:
                item["inline_source"] = cache[key]
                continue
            if enriched >= self._INLINE_MAX_ITEMS:
                continue  # past the cap: leave anchor-only (no new bodies)
            cache[key] = src
            item["inline_source"] = src
            enriched += 1
        return items

    def _compact_wire(self, envelope: dict) -> str:
        """Compact an envelope to the no-whitespace JSON string sent on the wire.

        Items are shrunk per :func:`budget.compact_item` (drop empties, alias
        verbose keys, truncate free text); a one-line ``legend`` mapping the
        aliases back to full names is added so the model reads the short keys
        unambiguously. Null/false envelope fields are dropped. The result is a
        string so FastMCP emits it verbatim instead of pretty-printing.
        """
        items = envelope.get("items", [])
        compact_items = [budget.compact_item(it) for it in items]
        # Only advertise aliases that actually appear, to keep the legend small.
        used = {k for it in compact_items if isinstance(it, dict) for k in it}
        legend = {
            alias: full
            for full, alias in budget.KEY_ALIASES.items()
            if alias in used
        }
        wire: dict = {"items": compact_items}
        if envelope.get("truncated"):
            wire["truncated"] = True
            if envelope.get("next_page_token"):
                wire["next_page_token"] = envelope["next_page_token"]
        if envelope.get("error"):
            wire["error"] = envelope["error"]
        if legend:
            wire["legend"] = legend
        return budget.compact_serialize(wire)

    def _index_state(self) -> dict:
        """Compute the envelope index_state; never raises."""
        try:
            return staleness.index_state(env.index_dir(), env.repo_root())
        except Exception:
            return {"built_at": None, "commit": None, "stale_files": 0, "note": None}

    def _log_call(
        self,
        tool_name: str,
        args: tuple,
        kwargs: dict,
        envelope: dict,
        start: float,
    ) -> None:
        """Append one JSON line to $RUN_DIR/mcp_calls.jsonl (best-effort)."""
        call_args: dict = dict(kwargs)
        if args:
            call_args["_positional"] = list(args)
        record = {
            "ts": time.time(),
            "server": self.name,
            "tool": tool_name,
            "args": call_args,
            "n_items": len(envelope["items"]),
            "truncated": envelope["truncated"],
            "stale_files": envelope["index_state"]["stale_files"],
            "duration_ms": round((time.perf_counter() - start) * 1000.0, 3),
        }
        try:
            run_dir = env.run_dir()
            run_dir.mkdir(parents=True, exist_ok=True)
            with open(run_dir / "mcp_calls.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass
