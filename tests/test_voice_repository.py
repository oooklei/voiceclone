"""Minimal CRUD smoke tests for the SQLite voice repository.

Full suite lands in Todo 9; this file carries the Todo 2 acceptance
assertions: create/get/update/enable/disable/delete, provider CHECK
constraint, and strict-schema extra-field rejection.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from app.repositories.voice_repository import VoiceRepository
from app.schemas import VoiceCreate, VoiceUpdate


@pytest.fixture()
def repo(tmp_path) -> VoiceRepository:
    return VoiceRepository(db_path=str(tmp_path / "voices.db"))


def test_crud_smoke(repo: VoiceRepository) -> None:
    created = repo.create(VoiceCreate(name="smoke", provider="cloud"))
    assert created.status == "draft"
    assert created.provider == "cloud"
    assert created.speed_ratio == 1.0

    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.name == "smoke"

    updated = repo.update(created.id, VoiceUpdate(speed_ratio=0.9, name="smoke2"))
    assert updated is not None
    assert updated.speed_ratio == 0.9
    assert updated.name == "smoke2"

    enabled = repo.enable(created.id)
    assert enabled is not None
    assert enabled.status == "active"

    disabled = repo.disable(created.id)
    assert disabled is not None
    assert disabled.status == "disabled"

    assert repo.delete(created.id) is True
    assert repo.get(created.id) is None


def test_list_filter_by_provider(repo: VoiceRepository) -> None:
    repo.create(VoiceCreate(name="c1", provider="cloud"))
    repo.create(VoiceCreate(name="l1", provider="local"))

    all_voices = repo.list()
    assert len(all_voices) == 2

    cloud = repo.list(provider="cloud")
    assert [v.name for v in cloud] == ["c1"]

    local = repo.list(provider="local")
    assert [v.name for v in local] == ["l1"]


def test_invalid_provider_rejected_by_schema(repo: VoiceRepository) -> None:
    # pydantic Literal rejects 'gpu' before the SQL layer
    with pytest.raises(ValidationError):
        VoiceCreate(name="bad", provider="gpu")  # type: ignore[arg-type]

    # raw SQL insert is stopped by the CHECK constraint
    with pytest.raises(sqlite3.IntegrityError):
        with repo._connect() as conn:
            conn.execute(
                "INSERT INTO voices (id, name, provider) VALUES ('x', 'bad', 'gpu')"
            )


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        VoiceCreate(name="x", bogus_field="nope")  # type: ignore[call-arg]
    assert "bogus_field" in str(exc_info.value)


def test_ratio_range_validation() -> None:
    with pytest.raises(ValidationError):
        VoiceCreate(name="x", speed_ratio=3.0)
    with pytest.raises(ValidationError):
        VoiceCreate(name="x", loudness_ratio=0.1)


def test_update_and_delete_missing_id(repo: VoiceRepository) -> None:
    assert repo.update("missing", VoiceUpdate(name="n")) is None
    assert repo.enable("missing") is None
    assert repo.disable("missing") is None
    assert repo.delete("missing") is False
