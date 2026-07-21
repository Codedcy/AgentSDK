from pydantic import BaseModel, ConfigDict


class RunFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    retryable: bool
