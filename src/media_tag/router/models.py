from dataclasses import dataclass
from typing import Annotated, Callable

from pydantic import Field, model_validator

from media_tag.contracts.models import ContractModel, Identifier, TaskEnvelope, TaskLimits, Text, Versions


class StepSpec(ContractModel):
    key: Identifier
    stage: Identifier
    depends_on: tuple[Identifier, ...] = ()
    config: dict = Field(default_factory=dict)
    versions: Versions
    target_fields: tuple[Text, ...] = ()
    limits: TaskLimits = Field(default_factory=TaskLimits)
    prompt_version: Text | None = None


class JobSpec(ContractModel):
    asset_id: Identifier
    steps: Annotated[tuple[StepSpec, ...], Field(min_length=1, max_length=64)]
    priority: Annotated[int, Field(ge=0, le=1)] = 0
    max_total_attempts: Annotated[int, Field(ge=1, le=256)] = 64

    @model_validator(mode="after")
    def check_dag(self):
        by_key = {step.key: step for step in self.steps}
        if len(by_key) != len(self.steps):
            raise ValueError("duplicate step key")
        visited, visiting = set(), set()
        def visit(key):
            if key not in by_key or key in visiting:
                raise ValueError("missing dependency or cyclic task DAG")
            if key in visited:
                return
            visiting.add(key)
            dependencies = by_key[key].depends_on
            if len(set(dependencies)) != len(dependencies):
                raise ValueError("duplicate dependency")
            for parent in dependencies:
                visit(parent)
            visiting.remove(key)
            visited.add(key)
        for key in by_key:
            visit(key)
        return self


class Lease(ContractModel):
    task: TaskEnvelope
    config: dict
    lease_until: float


@dataclass(frozen=True)
class StageDefinition:
    config_model: type[ContractModel]
    implementation_version: str
    gpu_allowed: bool = False
    metadata_sensitive: bool = True
    allow_disjoint_rebase: bool = False
    require_artifact: bool = True
    reduce_config: Callable[[dict], dict] | None = None
    synthetic: bool = False
    required_model_revision: str | None = None
    required_preprocess_version: str | None = None
    required_gpu_slots: int | None = None
    allowed_target_fields: tuple[str, ...] | None = None


class RouterOptions(ContractModel):
    cpu_slots: Annotated[int, Field(ge=1, le=16)] = 2
    lease_seconds: Annotated[float, Field(gt=0, le=600)] = 120.0
    heartbeat_seconds: Annotated[float, Field(gt=0, le=60)] = 15.0
    backoff_seconds: Annotated[float, Field(ge=0, le=300)] = 2.0
    interactive_bonus_seconds: Annotated[float, Field(ge=0, le=300)] = 30.0
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    max_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 300

    @model_validator(mode="after")
    def check_heartbeat(self):
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")
        return self
