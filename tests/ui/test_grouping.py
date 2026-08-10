"""Pure-function tests for the `-`-as-path-separator connection grouping."""

from __future__ import annotations

from dbctl.ui.grouping import GroupNode, build_connection_groups


def _index(nodes: list[GroupNode]) -> dict[str, GroupNode]:
    return {n.label: n for n in nodes}


def test_empty_list():
    assert build_connection_groups([]) == []


def test_no_dashes_stays_flat():
    groups = build_connection_groups(["pg", "mysql", "sqlite"])
    assert {n.label for n in groups} == {"pg", "mysql", "sqlite"}
    assert all(n.conn_name == n.label and not n.children for n in groups)


def test_single_child_chain_compresses_into_one_label():
    groups = build_connection_groups(["azure-sql"])
    assert len(groups) == 1
    node = groups[0]
    assert node.label == "azure-sql"
    assert node.path == "azure-sql"
    assert node.conn_name == "azure-sql"
    assert node.children == []


def test_branching_group_does_not_compress():
    groups = build_connection_groups(["prod-tenant1", "prod-tenant2"])
    assert len(groups) == 1
    prod = groups[0]
    assert prod.label == "prod"
    assert prod.conn_name is None  # "prod" alone isn't a connection
    assert {c.label for c in prod.children} == {"tenant1", "tenant2"}
    for c in prod.children:
        assert c.conn_name == f"prod-{c.label}"
        assert c.children == []


def test_name_that_is_also_a_prefix_of_others_is_dual_purpose():
    groups = build_connection_groups(["lookup", "lookup-dev", "lookup-test"])
    assert len(groups) == 1
    lookup = groups[0]
    assert lookup.label == "lookup"
    assert lookup.conn_name == "lookup"  # it IS a real connection too
    assert {c.label for c in lookup.children} == {"dev", "test"}


def test_deep_dual_purpose_chain_matches_real_world_naming():
    names = [
        "in-gateway",
        "in-gateway-claimsadvisor-dev",
        "in-gateway-dev",
        "in-gateway-ifp",
        "in-gateway-ifp-dev",
        "in-gateway-media-intake",
        "in-gateway-media-intake-dev",
    ]
    groups = build_connection_groups(names)
    assert len(groups) == 1
    top = groups[0]
    # "in" alone has exactly one next segment ("gateway") and isn't itself a
    # connection, so it compresses with "gateway" into one label - and that
    # combined node IS the "in-gateway" connection.
    assert top.label == "in-gateway"
    assert top.path == "in-gateway"
    assert top.conn_name == "in-gateway"

    by_label = _index(top.children)
    # "claimsadvisor" is not itself a connection and has exactly one next
    # segment ("dev"), so it folds forward into "claimsadvisor-dev" - the
    # same rule that produces "media-intake" below.
    assert set(by_label) == {"claimsadvisor-dev", "dev", "ifp", "media-intake"}

    assert by_label["dev"].conn_name == "in-gateway-dev"
    assert by_label["dev"].children == []

    assert by_label["claimsadvisor-dev"].conn_name == "in-gateway-claimsadvisor-dev"
    assert by_label["claimsadvisor-dev"].children == []

    # "ifp" is itself a connection AND has its own "-dev" child.
    assert by_label["ifp"].conn_name == "in-gateway-ifp"
    assert {c.label for c in by_label["ifp"].children} == {"dev"}

    # "media" + "intake" compress into one label since "media" alone isn't
    # a connection and has only one next segment ("intake").
    assert by_label["media-intake"].conn_name == "in-gateway-media-intake"
    assert {c.label for c in by_label["media-intake"].children} == {"dev"}


def test_unrelated_names_stay_separate_top_level_groups():
    names = ["mediaintake", "mediaintake-gateway", "media-intake"]
    groups = build_connection_groups(names)
    by_label = _index(groups)
    # "mediaintake" (no dash) and "media-intake" are unrelated strings -
    # splitting on "-" must not conflate them.
    assert set(by_label) == {"mediaintake", "media-intake"}
    assert by_label["mediaintake"].conn_name == "mediaintake"
    assert {c.label for c in by_label["mediaintake"].children} == {"gateway"}
    assert by_label["media-intake"].conn_name == "media-intake"
    assert by_label["media-intake"].children == []


def test_paths_are_stable_dash_joined_strings():
    groups = build_connection_groups(["a-b-c"])
    node = groups[0]
    assert node.path == "a-b-c"
    assert node.conn_name == "a-b-c"
