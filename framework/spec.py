"""The 'definition of done': a goal compiled into machine-verifiable checks + the environment they need."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CheckKind = Literal["dom", "timing", "visual", "code", "gpu", "http"]
GpuOp = Literal["attention", "matmul", "softmax", "layernorm"]
GpuDtype = Literal["fp16", "bf16", "fp32"]
GpuMetric = Literal["max_abs_err", "speedup_vs_torch", "tflops", "ms", "compiles"]


class Check(BaseModel):
    id: str = Field(description="Short stable id, e.g. C01")
    title: str
    kind: CheckKind = Field(description=(
        "dom: computed-style/DOM assertion run by Playwright; "
        "timing: measured by frame sampling; "
        "visual: Super VL judges screenshots against a rubric; "
        "code: file/HTML/console assertion; "
        "gpu: fixed harness runs the builder's kernel.py on a real GPU over SSH; "
        "http: the runner requests url (method) and compares against expected"))
    steps: list[str] = Field(description="Imperative steps the user-simulator performs before measuring")
    criteria: str = Field(description="Precise pass condition with numbers/hex/selectors where possible")
    selector: str | None = Field(default=None, description="CSS selector for dom/timing checks")
    property: str | None = Field(default=None, description="CSS property for dom checks")
    expected: str | None = Field(default=None, description="Expected value or range, e.g. '#1e1f22' or '150-250ms'")
    op: GpuOp | None = Field(default=None, description="gpu checks: which op kernel.py implements")
    shapes: dict[str, Any] | None = Field(default=None, description=(
        "gpu checks: problem size, e.g. {'batch':8,'heads':16,'seq':2048,'dim':64,'causal':false} "
        "or {'m':4096,'n':4096,'k':4096}"))
    dtype: GpuDtype | None = Field(default=None, description="gpu checks: input dtype")
    metric: GpuMetric | None = Field(default=None, description=(
        "gpu checks: what the harness measures; compared against expected (e.g. '< 2e-2', '>= 1.0', 'true')"))
    url: str | None = Field(default=None, description=(
        "http checks: absolute URL; may contain templates ${DEPLOY_URL}, ${NTFY_TOPIC}"))
    method: str | None = Field(default=None, description="http checks: GET (default) or POST")
    threshold: float = Field(default=0.7, description="Minimum score 0-1 for visual checks")
    weight: float = 1.0

    @field_validator("expected", mode="before")
    @classmethod
    def _expected_str(cls, v):  # LLMs sometimes emit true / 1.0 instead of "true" / ">= 1.0"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return v


class Environment(BaseModel):
    needs_browser: bool = True
    needs_desktop: bool = False
    needs_macos: bool = False
    needs_gpu: bool = False
    packages: list[str] = Field(default_factory=list, description="apt/pip packages beyond the base image")
    secrets: list[str] = Field(default_factory=list, description=(
        "canonical secret names from the catalog (framework.adapters.secrets_broker.CATALOG) the run needs"))
    devices: list[str] = Field(default_factory=list, description=(
        "free-text device manifest, e.g. 'sandbox-desktop', 'gpu-a100', 'macos'"))
    rationale: str = ""


class Spec(BaseModel):
    goal: str
    summary: str = ""
    checks: list[Check]
    environment: Environment = Field(default_factory=Environment)
    design_facts: list[str] = Field(default_factory=list,
                                    description="Concrete facts the builder must honour (colors, timings, fonts)")

    def score(self, results: dict[str, bool]) -> float:
        total = sum(c.weight for c in self.checks) or 1.0
        got = sum(c.weight for c in self.checks if results.get(c.id))
        return round(got / total, 3)
