"""Explicit opt-in for a local-code runtime, not a JavaScript sandbox."""
from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator


class ReplConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trusted_local_code: bool = False
    node_path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_enabled(cls, value):
        if isinstance(value, dict) and "enabled" in value:
            value = value.copy()
            # Existing profiles may explicitly forbid all local JS.
            if not TypeAdapter(bool).validate_python(value.pop("enabled")):
                value["trusted_local_code"] = False
        return value
