"""The profile registry picks a driving profile from the APK's package."""

from __future__ import annotations

from litmus.profiles import GENERIC, package_index, profile_for_package


def test_the_fixture_package_maps_to_its_profile() -> None:
    assert profile_for_package("com.bazaar.demo") == "bazaar"


def test_an_unknown_package_maps_to_nothing() -> None:
    assert profile_for_package("com.example.nowhere") is None


def test_missing_or_blank_package_maps_to_nothing() -> None:
    assert profile_for_package(None) is None
    assert profile_for_package("   ") is None


def test_the_generic_profile_claims_no_package() -> None:
    # `generic` is the fallback, not a match: indexing it would make every
    # upload look like it had been recognised.
    assert GENERIC not in package_index().values()


def test_index_is_cached_and_stable() -> None:
    # The API calls this per run; it must not re-read the directory each time.
    assert package_index() is package_index()
