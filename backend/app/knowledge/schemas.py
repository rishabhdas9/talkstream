from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EmbeddingModelResponse(BaseModel):
    id: str
    label: str
    dimensions: int
    default: bool


class EmbeddingProviderResponse(BaseModel):
    id: str
    label: str
    available: bool
    key_set: bool = False
    key_hint: str | None = None
    models: list[EmbeddingModelResponse]


class ProviderKeyBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)


class ProviderCatalogResponse(BaseModel):
    providers: list[EmbeddingProviderResponse]


class EmbeddingProfileBody(BaseModel):
    provider_id: str
    model_id: str


class EmbeddingProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    configured: bool
    provider_id: str
    model_id: str
    dimensions: int
    status: str
    active: bool
    generation_id: str | None = None
    reindex_processed_chunks: int | None = None
    reindex_total_chunks: int | None = None


class TestEmbeddingProfileResponse(BaseModel):
    ok: bool
    dimensions: int


class TextSourceBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1)


class KnowledgeSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    media_type: str
    status: str
    character_count: int
    chunk_count: int
    error_message: str | None
    created_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def public_status(cls, value):
        return "failed" if value == "deleting" else value


class KnowledgeSourceListResponse(BaseModel):
    sources: list[KnowledgeSourceResponse]


class KnowledgeSearchBody(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeSearchResult(BaseModel):
    chunk_id: str
    source_id: str
    source_name: str
    excerpt: str
    page_number: int | None
    score: float


class KnowledgeSearchResponse(BaseModel):
    results: list[KnowledgeSearchResult]
