"""Group connections by name, treating `-` as a path separator.

Real fleets tend to name connections hierarchically
(`in-gateway-ifp-dev`, `imageextractor-prod`, `lookup-test`) - flat, that's
a wall of near-duplicate strings. This builds a compressed prefix trie over
`name.split("-")` so the connection tree can show them nested instead.

A name can be *both* a group and a real connection at the same time (e.g.
`ifp` and `ifp-gateway`, or `lookup` and `lookup-dev`/`lookup-test` all
exist) - `GroupNode.conn_name` and `.children` are independent, so the tree
layer can render such a node as a connection that also has sub-groups.

Compression: a segment that is *not itself* a connection and has exactly
one next segment serves no navigational purpose as its own folder, so it
folds forward into that next segment (`azure` + `sql` -> one node labelled
`azure-sql`, not an `azure` folder wrapping a single `sql` leaf). A segment
that *is* a connection never folds away, even with one child, since
collapsing it would hide that it's independently selectable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GroupNode:
    label: str
    path: str
    conn_name: str | None
    children: list[GroupNode] = field(default_factory=list)


def build_connection_groups(connection_names: Iterable[str]) -> list[GroupNode]:
    names = list(connection_names)
    conn_set = set(names)

    # next_segments[prefix_tuple] = distinct next path segments seen after that prefix
    next_segments: dict[tuple[str, ...], set[str]] = {}
    for name in names:
        segments = tuple(name.split("-"))
        for i in range(len(segments)):
            next_segments.setdefault(segments[:i], set()).add(segments[i])

    def build_node(prefix: tuple[str, ...], label_parts: list[str]) -> GroupNode:
        """`prefix` is the path so far; `label_parts` accumulates the
        segments folded into this node's display label via compression."""
        is_connection = "-".join(prefix) in conn_set
        next_opts = sorted(next_segments.get(prefix, ()))
        if not is_connection and len(next_opts) == 1:
            return build_node(prefix + (next_opts[0],), [*label_parts, next_opts[0]])

        full_path = "-".join(prefix)
        children = [build_node(prefix + (seg,), [seg]) for seg in next_opts]
        return GroupNode(
            label="-".join(label_parts),
            path=full_path,
            conn_name=full_path if is_connection else None,
            children=children,
        )

    return [build_node((seg,), [seg]) for seg in sorted(next_segments.get((), ()))]
