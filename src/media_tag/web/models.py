from typing import Annotated
from pydantic import Field
from media_tag.contracts.models import ContractModel, Identifier, Sha256


class Enrollment(ContractModel):
    request_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    output_sha256: Sha256
    recipients: Annotated[tuple[Identifier, ...], Field(max_length=100)] = ()
    active: bool = True


class Retry(ContractModel):
    expected_revision: Annotated[int, Field(ge=1)]


class ReviewAssignment(Retry):
    output_sha256: Sha256
    subject_id: Identifier
