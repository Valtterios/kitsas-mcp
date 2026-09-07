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
