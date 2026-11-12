"""Operator-defined AI text fields appended to the original topic contract."""
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from pydantic import Field
from media_tag.contracts.models import ContractModel
from media_tag.contracts.templates import FieldDefinition, read_local_file
from media_tag.catalog.service import encode
from .contracts import TOPIC_FIELDS

DEFAULT_FIELDS_FILE = Path(__file__).with_name("tag-fields.json")


class TagField(ContractModel):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r'^[\p{L}\p{N}][\p{L}\p{N} _/-]*$')]
    prompt: Annotated[str, Field(min_length=1, max_length=1500, pattern=r'\S')]


class TagFields(ContractModel):
    fields: Annotated[tuple[TagField, ...], Field(max_length=32)] = ()

    @staticmethod
    def local_path(filename):
        from media_tag.storage.filesystem import require_local_path
        path = Path(filename)
        require_local_path(path)
        if not path.is_absolute():
            raise ValueError("tag_fields_file must be an absolute local path")
        return path

    @classmethod
    def load(cls, filename, reserved):
        if filename is None:
            return cls()
        path = cls.local_path(filename)
        registry = cls.model_validate_json(read_local_file(path.parent, path.name))
        names = [field.name for field in registry.fields]
        if (any(name != name.strip() for name in names) or len(set(names)) != len(names)
                or set(names).intersection((*reserved, *TOPIC_FIELDS))):
            raise ValueError("configured tag field names must be unique and distinct from original columns")
        return registry

    @property
    def guidance(self):
        return {field.name: field.prompt for field in self.fields}

    def definitions(self):
        return {field.name: FieldDefinition(field.name, '文本', '', 'AI初稿', 'ai_draft_human_sample_review',
                                           '扩展打标字段', None) for field in self.fields}


def topic_names(policy):
    if policy is None:
        return TOPIC_FIELDS
    return (*TOPIC_FIELDS, *policy.tag_fields.guidance)


def rules_digest(policy, fields):
    """Adding an unrelated field must preserve existing tasks and verified reports."""
    selected = {name: prompt for name, prompt in policy.tag_fields.guidance.items() if name in fields}
    if not selected:
        return policy.digest
    return sha256(encode([policy.digest, selected]).encode()).hexdigest()
