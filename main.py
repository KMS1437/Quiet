from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, ConfigDict
from typing import List, Dict, Optional
import time
import uuid
import hashlib
import json
from datetime import datetime, timezone
from collections import defaultdict
import math

from sqlalchemy import create_engine, Column, String, ForeignKey, DateTime, Table, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

SQLALCHEMY_DATABASE_URL = "sqlite:///./pulse.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

post_likes = Table(
    "post_likes",
    Base.metadata,
    Column("user_id", String, ForeignKey("users.id"), primary_key=True),
    Column("post_id", String, ForeignKey("posts.id"), primary_key=True)
)


class DBUser(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)

    profile = relationship("DBProfile", back_populates="user", uselist=False)
    posts = relationship("DBPost", back_populates="author")
    liked_posts = relationship(
        "DBPost",
        secondary=post_likes,
        back_populates="liked_by_users"
    )


class DBProfile(Base):
    __tablename__ = "profiles"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    email = Column(String)
    preferred_tags_json = Column(Text, default="{}")
    created_at = Column(DateTime)

    user = relationship("DBUser", back_populates="profile")


class DBPost(Base):
    __tablename__ = "posts"

    id = Column(String, primary_key=True, index=True)
    author_id = Column(String, ForeignKey("users.id"))
    content = Column(Text)
    timestamp = Column(DateTime)
    tags_json = Column(Text, default="[]")
    related_post_id = Column(String, nullable=True)
    # НОВОЕ ПОЛЕ
    public_analysis = Column(Boolean, default=False)

    author = relationship("DBUser", back_populates="posts")
    liked_by_users = relationship(
        "DBUser",
        secondary=post_likes,
        back_populates="liked_posts"
    )


class DBToken(Base):
    __tablename__ = "tokens"

    token = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"))


# Создаём таблицы (если поле public_analysis новое, SQLAlchemy добавит его автоматически при create_all)
Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Pydantic модели ----------
class UserBase(BaseModel):
    email: EmailStr


class UserIn(UserBase):
    password: str


class UserProfile(UserBase):
    user_id: str
    preferred_tags: Dict[str, int] = {}
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class PostIn(BaseModel):
    content: str
    tags: List[str]
    related_post_id: Optional[str] = None
    public_analysis: bool = False  # новое поле


class PostOut(PostIn):
    id: str
    author_id: str
    timestamp: datetime
    likes: List[str] = []
    score: Optional[float] = 0.0
    model_config = ConfigDict(from_attributes=True)


class LoginResponse(BaseModel):
    token: str
    user_id: str


class GraphNode(BaseModel):
    id: str
    name: str
    val: int
    group: str


class GraphLink(BaseModel):
    source: str
    target: str


class GraphData(BaseModel):
    nodes: List[GraphNode]
    links: List[GraphLink]


# Модель для рекомендаций сообщества
class CommunityUser(BaseModel):
    user_id: str
    similarity: float
    common_tags: List[str]
    public_posts_count: int


# ---------- Утилиты ----------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_uuid() -> str:
    return str(uuid.uuid4())


def calculate_recommendations(all_posts: List[DBPost], preferred_tags: Dict[str, int]) -> List[PostOut]:
    scored_posts = []
    now = time.time()
    for db_post in all_posts:
        score = 0
        tags = json.loads(db_post.tags_json or "[]")
        for tag in tags:
            score += preferred_tags.get(tag.lower(), 0)
        post_ts = db_post.timestamp.replace(
            tzinfo=timezone.utc
        ).timestamp() if db_post.timestamp.tzinfo else db_post.timestamp.timestamp()
        time_diff_hours = (now - post_ts) / 3600
        recency_factor = max(0.1, 1.5 - (time_diff_hours / 48))
        score *= recency_factor
        likes_ids = [u.id for u in db_post.liked_by_users]
        pydantic_post = PostOut(
            id=db_post.id,
            author_id=db_post.author_id,
            timestamp=db_post.timestamp,
            content=db_post.content,
            tags=tags,
            related_post_id=db_post.related_post_id,
            public_analysis=db_post.public_analysis,
            likes=likes_ids,
            score=round(score, 2)
        )
        scored_posts.append(pydantic_post)
    scored_posts.sort(key=lambda x: (x.score, x.timestamp), reverse=True)
    return scored_posts


# ---------- FastAPI ----------
app = FastAPI(title="Pulse API SQL")

origins = [
    "http://127.0.0.1",
    "http://localhost",
    "http://localhost:63342",
    "http://127.0.0.1:63342",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:3000",
    "http://localhost:8080"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class NotAuthenticated(HTTPException):
    def __init__(self, detail: str = "Не аутентифицирован"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> str:
    token_str = request.headers.get("Authorization")
    if not token_str:
        raise NotAuthenticated("Отсутствует Authorization.")
    if token_str.startswith("Bearer "):
        token_str = token_str.split(" ")[1]
    db_token = db.query(DBToken).filter(DBToken.token == token_str).first()
    if not db_token:
        raise NotAuthenticated("Токен недействителен.")
    return db_token.user_id


@app.get("/")
def read_root():
    return {"message": "Pulse API with SQL is running."}


@app.post("/register", response_model=LoginResponse)
def register_user(user_data: UserIn, db: Session = Depends(get_db)):
    existing_user = db.query(DBUser).filter(DBUser.email == user_data.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email занят.")
    user_id = create_uuid()
    hashed_password = hash_password(user_data.password)
    new_user = DBUser(
        id=user_id,
        email=user_data.email,
        hashed_password=hashed_password
    )
    db.add(new_user)
    new_profile = DBProfile(
        user_id=user_id,
        email=user_data.email,
        preferred_tags_json="{}",
        created_at=datetime.now(timezone.utc)
    )
    db.add(new_profile)
    token_str = create_uuid()
    new_token = DBToken(token=token_str, user_id=user_id)
    db.add(new_token)
    db.commit()
    return LoginResponse(token=token_str, user_id=user_id)


@app.post("/login", response_model=LoginResponse)
def login_user(user_data: UserIn, db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.email == user_data.email).first()
    if not user or user.hashed_password != hash_password(user_data.password):
        raise HTTPException(status_code=401, detail="Неверные учетные данные.")
    token_str = create_uuid()
    new_token = DBToken(token=token_str, user_id=user.id)
    db.add(new_token)
    db.commit()
    return LoginResponse(token=token_str, user_id=user.id)


@app.post("/posts/create", response_model=PostOut)
def create_post(post_data: PostIn, user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    post_id = create_uuid()
    processed_tags = [t.strip().lower() for t in post_data.tags if t.strip()]
    new_post = DBPost(
        id=post_id,
        author_id=user_id,
        timestamp=datetime.now(timezone.utc),
        content=post_data.content,
        tags_json=json.dumps(processed_tags),
        related_post_id=post_data.related_post_id,
        public_analysis=post_data.public_analysis  # новое поле
    )
    db.add(new_post)
    db.commit()
    db.refresh(new_post)
    return PostOut(
        id=new_post.id,
        author_id=new_post.author_id,
        timestamp=new_post.timestamp,
        content=new_post.content,
        tags=processed_tags,
        related_post_id=new_post.related_post_id,
        public_analysis=new_post.public_analysis,
        likes=[],
        score=0.0
    )


@app.get("/posts", response_model=List[PostOut])
def get_feed(user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(DBProfile).filter(DBProfile.user_id == user_id).first()
    if not profile:
        raise NotAuthenticated("Профиль не найден.")
    all_posts = db.query(DBPost).all()
    preferred_tags = json.loads(profile.preferred_tags_json or "{}")
    final_feed = calculate_recommendations(all_posts, preferred_tags)
    return final_feed


@app.post("/posts/{post_id}/like")
def toggle_like(post_id: str, user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    post = db.query(DBPost).filter(DBPost.id == post_id).first()
    user = db.query(DBUser).filter(DBUser.id == user_id).first()
    profile = db.query(DBProfile).filter(DBProfile.user_id == user_id).first()
    if not post or not user or not profile:
        raise HTTPException(404, "Объект не найден")
    if user in post.liked_by_users:
        post.liked_by_users.remove(user)
        is_liking = False
    else:
        post.liked_by_users.append(user)
        is_liking = True
    preferred_tags = json.loads(profile.preferred_tags_json or "{}")
    post_tags = json.loads(post.tags_json or "[]")
    for tag in post_tags:
        curr = preferred_tags.get(tag, 0)
        if is_liking:
            preferred_tags[tag] = curr + 1
        else:
            new_val = max(0, curr - 1)
            if new_val == 0:
                preferred_tags.pop(tag, None)
            else:
                preferred_tags[tag] = new_val
    profile.preferred_tags_json = json.dumps(preferred_tags)
    db.commit()
    db.refresh(post)
    return {"status": "success", "likes": len(post.liked_by_users)}


@app.get("/users/{user_id}/graph", response_model=GraphData)
def get_user_graph(user_id: str, db: Session = Depends(get_db)):
    user_posts = db.query(DBPost).filter(DBPost.author_id == user_id).all()
    nodes = []
    links = []
    existing_ids = {p.id for p in user_posts}
    for post in user_posts:
        tags = json.loads(post.tags_json or "[]")
        group = tags[0] if tags else "general"
        short_name = post.content[:20]
        val = len(post.liked_by_users) + 1
        nodes.append(GraphNode(id=post.id, name=short_name, val=val, group=group))
        if post.related_post_id and post.related_post_id in existing_ids:
            links.append(GraphLink(source=post.id, target=post.related_post_id))
    return GraphData(nodes=nodes, links=links)


# ---------- НОВЫЕ ЭНДПОИНТЫ ДЛЯ СООБЩЕСТВ ----------

@app.post("/posts/{post_id}/toggle_public")
def toggle_post_public(post_id: str, user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    post = db.query(DBPost).filter(DBPost.id == post_id, DBPost.author_id == user_id).first()
    if not post:
        raise HTTPException(404, "Заметка не найдена или доступ запрещён")
    post.public_analysis = not post.public_analysis
    db.commit()
    return {"public_analysis": post.public_analysis}


@app.get("/community/recommendations", response_model=List[CommunityUser])
def get_community_recommendations(current_user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    # Получаем все публичные посты
    public_posts = db.query(DBPost).filter(DBPost.public_analysis == True).all()

    # Строим векторы тегов для каждого пользователя (на основе публичных постов)
    user_tag_vectors = defaultdict(lambda: defaultdict(float))
    user_public_counts = defaultdict(int)

    for post in public_posts:
        uid = post.author_id
        user_public_counts[uid] += 1
        tags = json.loads(post.tags_json or "[]")
        for tag in tags:
            user_tag_vectors[uid][tag] += 1.0

    # Нормализуем векторы (TF)
    for uid, vec in user_tag_vectors.items():
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            for tag in vec:
                vec[tag] /= norm

    current_vec = user_tag_vectors.get(current_user_id, {})
    if not current_vec:
        return []

    # Вычисляем косинусное сходство с другими пользователями
    similarities = []
    for other_uid, other_vec in user_tag_vectors.items():
        if other_uid == current_user_id:
            continue
        # Косинусное сходство
        dot = sum(current_vec.get(tag, 0) * val for tag, val in other_vec.items())
        # Нормы уже 1, поэтому similarity = dot
        similarity = dot
        # Общие теги (для отображения)
        common = [tag for tag in other_vec if tag in current_vec]
        common.sort(key=lambda t: other_vec[t], reverse=True)
        similarities.append({
            "user_id": other_uid,
            "similarity": similarity,
            "common_tags": common[:5],  # топ-5 общих тегов
            "public_posts_count": user_public_counts[other_uid]
        })

    # Сортируем по убыванию схожести
    similarities.sort(key=lambda x: x["similarity"], reverse=True)
    # Возвращаем топ-20
    return similarities[:20]
@app.get("/users/{user_id}")
def get_user_profile(user_id: str, db: Session = Depends(get_db)):
    profile = db.query(DBProfile).filter(DBProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    return {
        "user_id": profile.user_id,
        "email": profile.email,
        "preferred_tags": json.loads(profile.preferred_tags_json or "{}"),
        "created_at": profile.created_at.isoformat()
    }
