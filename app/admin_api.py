from fastapi import APIRouter, Depends, HTTPException, status

from . import user_store
from .auth import hash_password, require_admin
from .schemas import UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate) -> dict:
    try:
        return await user_store.create(body.username, hash_password(body.password))
    except ValueError:
        raise HTTPException(status.HTTP_409_CONFLICT, "username exists")


@router.get("", response_model=list[UserOut])
async def list_users() -> list[dict]:
    return await user_store.list_all()


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdate) -> dict:
    password_hash = hash_password(body.password) if body.password else None
    user = await user_store.update(user_id, password_hash=password_hash, is_active=body.is_active)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int) -> None:
    ok = await user_store.delete(user_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
