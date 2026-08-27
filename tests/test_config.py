from tamandua.config import load_pins, resolve_checkout


def test_loads_default_manifest_and_swatplus_version():
    pins = load_pins()
    assert pins.swatplus_version == "62.0.0"


def test_verified_pins_are_resolved():
    pins = load_pins()
    corpus = pins.get("reference_corpus")
    dataselector = pins.get("dataselector")
    assert corpus.resolved
    assert corpus.version() == "2daa14ae7b50c597aefbc110734ec5bfc5472cb0"
    assert dataselector.resolved
    assert dataselector.version() == "fd7af35"


def test_all_dependency_pins_resolved():
    # Every external dependency is pinned to a concrete commit/ref, so a build
    # -- including the release workflow's -- never floats to a branch tip.
    pins = load_pins()
    for section in ("reference_corpus", "dataselector", "swatplus_source",
                    "reference_dataset"):
        assert pins.get(section).resolved, f"{section} should be resolved"
    assert pins.unresolved() == []


def test_release_workflow_can_read_what_it_builds_against():
    # .github/workflows/release.yml reads exactly these two to check out the
    # source and the parser. A rename here breaks the release, not the tests,
    # so assert the shape the workflow depends on.
    pins = load_pins()
    assert pins.raw["swatplus_source"]["ref"] == "62.0.0"
    assert pins.raw["reference_corpus"]["commit"] == \
        "2daa14ae7b50c597aefbc110734ec5bfc5472cb0"


def test_reference_dataset_is_resolved_to_ames():
    pins = load_pins()
    ames = pins.get("reference_dataset")
    assert ames.resolved
    assert ames.version() == "de210d6"  # swatplus tag 62.0.0
    assert pins.raw["reference_dataset"]["path_in_repo"] == "refdata/Ames_sub1"


def test_resolve_checkout_prefers_env_var(tmp_path, monkeypatch):
    checkout = tmp_path / "reference-corpus"
    checkout.mkdir()
    monkeypatch.setenv("SWATPLUS_REFERENCE_CORPUS", str(checkout))
    assert resolve_checkout("reference_corpus") == checkout


def test_resolve_checkout_returns_none_for_bad_env_path(monkeypatch):
    # An explicit override that does not exist must not silently fall back to
    # the conventional location — that would hide a misconfiguration.
    monkeypatch.setenv("SWATPLUS_REFERENCE_CORPUS", "/nonexistent/path")
    assert resolve_checkout("reference_corpus") is None


def test_resolve_checkout_unknown_section_is_none():
    assert resolve_checkout("not_a_dependency") is None
