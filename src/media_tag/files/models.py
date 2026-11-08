from typing import Annotated
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel


class PlaybackInterval(ContractModel):
    start_seconds: Annotated[float,Field(ge=0)]
    end_seconds: Annotated[float,Field(gt=0)]

    @model_validator(mode='after')
    def ordered(self):
        if self.start_seconds>=self.end_seconds:raise ValueError('positive half-open interval required')
        return self
