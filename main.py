from datetime import date
from fastapi import FastAPI
from pydantic import BaseModel, EmailStr
from typing import List

app = FastAPI()


class UserRegistration(BaseModel):
    username: str
    birthday: date
    email: EmailStr
    password: str


class PostCreate(BaseModel):
    content: str
    tags: List[str] = []


@app.get("/users/test")
async def test_users():
    test_user = UserRegistration(
        username="test_user",
        birthday=date(1990, 5, 15),
        email="test@example.com",
        password="securepassword123"
    )

    return {
        "test_data": test_user,
        "model": "UserRegistration"
    }


@app.get("/posts/test")
async def test_posts():
    test_post = PostCreate(
        content="Это тестовый пост",
        tags=["тест", "демонстрация"]
    )

    return {
        "test_data": test_post,
        "model": "PostCreate"
    }
