from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_task_assistant.models.task import Task
from ai_task_assistant.schemas.task import TaskCreate, TaskUpdate


def create_task(db: Session, *, user_id: int, task_in: TaskCreate) -> Task:
    task = Task(user_id=user_id, **task_in.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def list_tasks(
    db: Session,
    *,
    user_id: int,
    status: str | None = None,
    due_after: datetime | None = None,
    due_before: datetime | None = None,
) -> list[Task]:
    statement = select(Task).where(Task.user_id == user_id)
    if status is not None:
        statement = statement.where(Task.status == status)
    if due_after is not None:
        statement = statement.where(Task.due_date >= due_after)
    if due_before is not None:
        statement = statement.where(Task.due_date <= due_before)

    return list(db.scalars(statement.order_by(Task.id)))


def get_task(db: Session, *, user_id: int, task_id: int) -> Task | None:
    return db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))


def update_task(db: Session, *, user_id: int, task_id: int, task_in: TaskUpdate) -> Task | None:
    task = get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        return None
    for field, value in task_in.model_dump(exclude_unset=True).items():
        setattr(task, field, value)

    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, *, user_id: int, task_id: int) -> bool:
    task = get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        return False
    db.delete(task)
    db.commit()
    return True
