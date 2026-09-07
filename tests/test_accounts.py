import json
import pytest

from kitsas_mcp.accounts import default_bank_account, default_payable_account, get_account, list_accounts
from kitsas_mcp.errors import AccountNotFoundError


def test_lists_every_account_with_its_finnish_name(book):
    accounts = list_accounts(book)
    assert len(accounts) == 5
    assert {"number": 1910, "name": "Pankkitili", "type": "ARP"} in accounts


def test_accounts_are_ordered_by_number(book):
    numbers = [a["number"] for a in list_accounts(book)]
    assert numbers == sorted(numbers)


def test_search_matches_the_name_case_insensitively(book):
    assert [a["number"] for a in list_accounts(book, "edustus")] == [4590]


def test_search_matches_the_account_number(book):
    assert [a["number"] for a in list_accounts(book, "4590")] == [4590]


def test_get_account_returns_one(book):
    assert get_account(book, 4000)["name"] == "Tavaraostot, varsinainen toiminta"


def test_get_account_names_the_missing_number(book):
    with pytest.raises(AccountNotFoundError) as excinfo:
        get_account(book, 9999)
    assert "9999" in str(excinfo.value)


def test_default_bank_account_is_the_arp_account(book):
    assert default_bank_account(book) == 1910


def test_default_payable_account_is_the_bo_account(book):
    assert default_payable_account(book) == 2960


def test_list_accounts_with_malformed_json(book):
    """Malformed JSON should degrade gracefully, not crash list_accounts."""
    with book.connect_write() as conn:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (5000, "DZ", "{broken"),
        )
    accounts = list_accounts(book)
    assert len(accounts) == 6
    malformed = [a for a in accounts if a["number"] == 5000][0]
    assert malformed["name"] == ""
    assert malformed["type"] == "DZ"


# -- Finding 2: valid JSON that is not the object shape expected -------------


def test_list_accounts_with_json_null(book):
    """json = 'null' parses without error, but data.get('nimi') on None raises AttributeError."""
    with book.connect_write() as conn:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (5001, "DZ", "null"),
        )
    accounts = list_accounts(book)
    account = [a for a in accounts if a["number"] == 5001][0]
    assert account["name"] == ""
    assert account["type"] == "DZ"


def test_list_accounts_with_json_list(book):
    """A JSON array is valid JSON but not a dict either."""
    with book.connect_write() as conn:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (5002, "DZ", "[1, 2, 3]"),
        )
    accounts = list_accounts(book)
    account = [a for a in accounts if a["number"] == 5002][0]
    assert account["name"] == ""
    assert account["type"] == "DZ"


def test_list_accounts_with_nimi_as_a_plain_string(book):
    """'nimi' is supposed to be {'fi': ..., 'sv': ...}; a bare string would make names.get('fi') raise."""
    with book.connect_write() as conn:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (5003, "DZ", json.dumps({"nimi": "just a string"})),
        )
    accounts = list_accounts(book)
    account = [a for a in accounts if a["number"] == 5003][0]
    assert account["name"] == ""
    assert account["type"] == "DZ"


def test_number_search_uses_prefix_matching(book):
    """Number search should match prefixes, not substrings."""
    with book.connect_write() as conn:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (3190, "DZ", json.dumps({"nimi": {"fi": "Kolmannen lajin tili"}})),
        )
    # 19 should match 1910 (starts with 19) but not 3190 (contains 19)
    assert [a["number"] for a in list_accounts(book, "19")] == [1910]


def test_number_search_exact_match(book):
    """Exact number match should work as before."""
    assert [a["number"] for a in list_accounts(book, "4590")] == [4590]


def test_default_bank_account_with_multiple_candidates(book):
    """When multiple bank accounts exist, default_bank_account should raise."""
    with book.connect_write() as conn:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (1911, "ARP", json.dumps({"nimi": {"fi": "Toinen pankkitili"}})),
        )
    with pytest.raises(AccountNotFoundError) as excinfo:
        default_bank_account(book)
    error_msg = str(excinfo.value)
    assert "1910" in error_msg
    assert "1911" in error_msg


def test_default_payable_account_with_multiple_candidates(book):
    """When multiple payable accounts exist, default_payable_account should raise."""
    with book.connect_write() as conn:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (2961, "BO", json.dumps({"nimi": {"fi": "Toiset ostovelat"}})),
        )
    with pytest.raises(AccountNotFoundError) as excinfo:
        default_payable_account(book)
    error_msg = str(excinfo.value)
    assert "2960" in error_msg
    assert "2961" in error_msg
