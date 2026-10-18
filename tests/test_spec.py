import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.spec import Check, Spec

FIXTURE = Path(__file__).parent / "fixture_spec.json"
ALLOWED = {"dom", "timing", "visual", "code"}


@pytest.fixture(scope="module")
def spec() -> Spec:
    return Spec.model_validate(json.loads(FIXTURE.read_text()))


def test_fixture_loads(spec):
    assert spec.goal
    assert 8 <= len(spec.checks) <= 14
    assert len({c.id for c in spec.checks}) == len(spec.checks)
    assert spec.environment.needs_browser
    assert not spec.environment.needs_macos


def test_kinds_allowed(spec):
    assert {c.kind for c in spec.checks} <= ALLOWED


def test_dom_checks_complete(spec):
    doms = [c for c in spec.checks if c.kind == "dom"]
    assert doms
    for c in doms:
        assert c.selector and c.property and c.expected, c.id


def test_score_weighting():
    mk = lambda i, w: Check(id=i, title=i, kind="dom", steps=["goto /"], criteria="x", weight=w)
    s = Spec(goal="g", checks=[mk("A", 1.0), mk("B", 3.0)])
    assert s.score({}) == 0.0
    assert s.score({"A": True}) == 0.25
    assert s.score({"B": True}) == 0.75
    assert s.score({"A": True, "B": True}) == 1.0
    assert s.score({"A": False, "B": True, "Z": True}) == 0.75


def test_score_fixture(spec):
    all_pass = {c.id: True for c in spec.checks}
    assert spec.score(all_pass) == 1.0
    first = spec.checks[0].id
    assert 0 < spec.score({first: True}) < 1


def test_invalid_kind_rejected():
    with pytest.raises(Exception):
        Check(id="X", title="x", kind="vibes", steps=[], criteria="x")
