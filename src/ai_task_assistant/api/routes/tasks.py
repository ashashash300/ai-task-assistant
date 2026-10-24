from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.orm import Session

from ai_task_assistant.api.dependencies.auth import (
    require_csrf_protection,
    require_current_user,
)
from ai_task_assistant.db.session import get_db
from ai_task_assistant.models.user import User
from ai_task_assistant.schemas.task import TaskCreate, TaskResponse, TaskUpdate
from ai_task_assistant.services import task_service


router = APIRouter(prefix="/tasks", tags=["tasks"])
TaskId = Annotated[int, Path(gt=0)]


def get_existing_task(db: Session, user_id: int, task_id: int):
    task = task_service.get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    task_in: TaskCreate,
    current_user: Annotated[User, Depends(require_current_user)],
    _csrf: Annotated[None, Depends(require_csrf_protection)],
    db: Session = Depends(get_db),
) -> TaskResponse:
    return task_service.create_task(db, user_id=current_user.id, task_in=task_in)


@router.get("", response_model=list[TaskResponse])
def list_tasks(
    current_user: Annotated[User, Depends(require_current_user)],
    db: Session = Depends(get_db),
) -> list[TaskResponse]:
    return task_service.list_tasks(db, user_id=current_user.id)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: TaskId,
    current_user: Annotated[User, Depends(require_current_user)],
    db: Session = Depends(get_db),
) -> TaskResponse:
    return get_existing_task(db, current_user.id, task_id)


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: TaskId,
    task_in: TaskUpdate,
    current_user: Annotated[User, Depends(require_current_user)],
    _csrf: Annotated[None, Depends(require_csrf_protection)],
    db: Session = Depends(get_db),
) -> TaskResponse:
    task = task_service.update_task(
        db, user_id=current_user.id, task_id=task_id, task_in=task_in
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: TaskId,
    current_user: Annotated[User, Depends(require_current_user)],
    _csrf: Annotated[None, Depends(require_csrf_protection)],
    db: Session = Depends(get_db),
) -> Response:
    if not task_service.delete_task(db, user_id=current_user.id, task_id=task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
