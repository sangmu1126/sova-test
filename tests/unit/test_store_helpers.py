"""Pure decisions the store adapters rest on, without a server: which MongoDB
privileges let a user change a database, how a Redis ACL line splits into
rules, and what counts as a plain key prefix."""

import pytest

from sova.adapters.mongodb_handoff import account, may_write
from sova.adapters.redis_keyspace_handoff import acl_rules, prefix
from sova.domain.contracts import ContractError


@pytest.mark.parametrize("privileges, expected", [
    ([{"resource": {"db": "orders", "collection": ""}, "actions": ["find", "insert"]}], True),
    ([{"resource": {"db": "orders", "collection": "orders"}, "actions": ["update"]}], True),
    ([{"resource": {"db": "", "collection": ""}, "actions": ["remove"]}], True),  # any db
    ([{"resource": {"anyResource": True}, "actions": ["anyAction", "dropDatabase"]}], True),
    ([{"resource": {"db": "orders", "collection": ""}, "actions": ["find", "listIndexes"]}],
     False),
    ([{"resource": {"db": "shop", "collection": ""}, "actions": ["insert"]}], False),
    ([{"resource": {"cluster": True}, "actions": ["serverStatus"]}], False),
    ([], False), (None, False),
])
def test_only_privileges_that_change_the_database_count_as_writing(privileges, expected):
    assert may_write(privileges, "orders") is expected


def test_a_writer_is_named_with_its_authentication_database():
    assert account("orders-app@admin") == ("orders-app", "admin")
    for bad in ("orders-app", "orders app@admin", "app@ad min", ""):
        with pytest.raises(ContractError):
            account(bad)


def test_an_acl_line_keeps_a_selector_whole():
    name, rules = acl_rules("user board-app on #ab12 ~board:* resetchannels -@all "
                            "+@read (~audit:* +get +set)")
    assert name == "board-app"
    assert rules == ["on", "#ab12", "~board:*", "resetchannels", "-@all", "+@read",
                     "(~audit:* +get +set)"]
    with pytest.raises(ContractError):
        acl_rules("role admins +@all")


@pytest.mark.parametrize("value, plain", [("board:", "board:"), ("board:*", "board:")])
def test_a_prefix_is_plain(value, plain):
    assert prefix(value) == plain


@pytest.mark.parametrize("value", ["board:*:sessions", "board:[ab]", "board:?", "board: x",
                                   "", "a\\b", 42])
def test_a_glob_or_anything_else_is_not_a_prefix(value):
    with pytest.raises(ContractError):
        prefix(value)
