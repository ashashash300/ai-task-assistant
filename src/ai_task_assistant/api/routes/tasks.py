from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.orm import Session

from ai_task_assistant.db.session import get_db
from ai_task_assistant.schemas.task import TaskCreate, TaskResponse, TaskUpdate
from ai_task_assistant.services import task_service


router = APIRouter(prefix="/tasks", tags=["tasks"])
TaskId = Annotated[int, Path(gt=0)]


def get_existing_task(db: Session, task_id: int):
    task = task_service.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(task_in: TaskCreate, db: Session = Depends(get_db)) -> TaskResponse:
    return task_service.create_task(db, task_in)


@router.get("", response_model=list[TaskResponse])
def list_tasks(db: Session = Depends(get_db)) -> list[TaskResponse]:
    return task_service.list_tasks(db)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: TaskId, db: Session = Depends(get_db)) -> TaskResponse:
    return get_existing_task(db, task_id)


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(task_id: TaskId, task_in: TaskUpdate, db: Session = Depends(get_db)) -> TaskResponse:
    task = get_existing_task(db, task_id)
    return task_service.update_task(db, task, task_in)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: TaskId, db: Session = Depends(get_db)) -> Response:
    task = get_existing_task(db, task_id)
    task_service.delete_task(db, task)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
