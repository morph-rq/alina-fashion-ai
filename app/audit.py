from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_change(
    session: Session,
    *,
    entity_type: str,
    entity_id: int | str,
    field: str | None,
    previous_value: object,
    new_value: object,
    source_user: str,
    description: str,
) -> None:
    session.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=str(entity_id),
            field=field,
            previous_value=None if previous_value is None else str(previous_value),
            new_value=None if new_value is None else str(new_value),
            source_user=source_user,
            description=description,
        )
    )
