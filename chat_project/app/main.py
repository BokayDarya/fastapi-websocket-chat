from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from contextlib import asynccontextmanager
from typing import List
import json
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext

from app import models, schemas
from app.database import engine, get_db
from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    yield

app = FastAPI(title="Microservice Chat API", lifespan=lifespan)

# --- Управление подключениями WebSocket ---
class ConnectionManager:
    def __init__(self):
        # Храним не только вебсокет, но и имя пользователя
        self.active_connections: dict[int, dict] = {}

    async def connect(self, websocket: WebSocket, user_id: int, username: str):
        await websocket.accept()
        self.active_connections[user_id] = {"ws": websocket, "username": username}
        await self.broadcast_active_users() # Рассылаем обновленный список онлайн

    async def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        await self.broadcast_active_users() # Рассылаем обновленный список онлайн

    async def broadcast(self, message: str):
        for connection in self.active_connections.values():
            try:
                await connection["ws"].send_text(message)
            except:
                pass

    async def broadcast_active_users(self):
        # Отправляем системное сообщение со списком онлайна
        users_list = [{"id": uid, "username": data["username"]} for uid, data in self.active_connections.items()]
        msg = json.dumps({"type": "active_users", "users": users_list})
        await self.broadcast(msg)

    async def kick_user(self, user_id: int):
        # Принудительное закрытие соединения забаненного пользователя
        if user_id in self.active_connections:
            ws = self.active_connections[user_id]["ws"]
            try:
                await ws.send_text(json.dumps({"banned": True, "text": "Вы были заблокированы администратором."}))
                await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            except:
                pass

manager = ConnectionManager()

# --- Вспомогательные функции авторизации ---
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Неверный токен")
    except JWTError:
        raise HTTPException(status_code=401, detail="Неверный токен")
    
    result = await db.execute(select(models.User).where(models.User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user

async def get_user_ws(token: str, db: AsyncSession):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        result = await db.execute(select(models.User).where(models.User.username == username))
        return result.scalar_one_or_none()
    except:
        return None

# --- RESTful Эндпоинты ---
@app.get("/", response_class=FileResponse)
async def get_chat_page():
    return "app/index.html"

@app.post("/register", response_model=schemas.UserResponse)
async def register(user: schemas.UserCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.User).where(models.User.username == user.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    
    hashed_pw = pwd_context.hash(user.password)
    db_user = models.User(username=user.username, hashed_password=hashed_pw, is_admin=user.is_admin)
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.User).where(models.User.username == form_data.username))
    user = result.scalar_one_or_none()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Неверное имя пользователя или пароль")
    
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Ваш аккаунт был заблокирован администратором")
    
    token = create_access_token(data={"sub": user.username})
    return {"access_token": token, "token_type": "bearer", "user_id": user.id, "username": user.username, "is_admin": user.is_admin}

@app.delete("/messages/{message_id}")
async def delete_message(message_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    
    result = await db.execute(select(models.Message).where(models.Message.id == message_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    
    await db.delete(msg)
    await db.commit()
    await manager.broadcast(json.dumps({"delete": True, "message_id": message_id}))
    return {"status": "Успешно удалено"}

@app.post("/admin/ban/{target_user_id}")
async def ban_user(target_user_id: int, current_user: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    
    result = await db.execute(select(models.User).where(models.User.id == target_user_id))
    target_user = result.scalar_one_or_none()
    
    if not target_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target_user.is_admin:
        raise HTTPException(status_code=403, detail="Нельзя заблокировать администратора")
        
    target_user.is_banned = True
    await db.commit()
    
    # Принудительно выкидываем из чата
    await manager.kick_user(target_user_id)
    await manager.broadcast(json.dumps({"system": True, "text": f"Пользователь {target_user.username} был заблокирован."}))
    return {"status": "Заблокирован"}


# --- WebSocket Эндпоинт ---
@app.websocket("/ws/chat/")
async def websocket_endpoint(websocket: WebSocket, token: str, db: AsyncSession = Depends(get_db)):
    user = await get_user_ws(token, db)
    if not user or user.is_banned:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket, user.id, user.username)
    try:
        # Отправка истории
        result = await db.execute(
            select(models.Message)
            .options(selectinload(models.Message.owner))
            .order_by(models.Message.id.asc())
            .limit(50)
        )
        history = result.scalars().all()
        for msg in history:
            time_str = msg.timestamp.strftime("%H:%M:%S")
            await websocket.send_text(json.dumps({
                "id": msg.id, "user_id": msg.owner_id, "username": msg.owner.username,
                "text": msg.content, "timestamp": time_str
            }))

        await manager.broadcast(json.dumps({"system": True, "text": f"Пользователь {user.username} вошел в чат."}))

        while True:
            data = await websocket.receive_text()
            new_msg = models.Message(content=data, owner_id=user.id)
            db.add(new_msg)
            await db.commit()
            await db.refresh(new_msg)
            
            time_str = new_msg.timestamp.strftime("%H:%M:%S")
            await manager.broadcast(json.dumps({
                "id": new_msg.id, "user_id": user.id, "username": user.username,
                "text": data, "timestamp": time_str
            }))

    except WebSocketDisconnect:
        await manager.disconnect(user.id)
        await manager.broadcast(json.dumps({"system": True, "text": f"Пользователь {user.username} покинул чат."}))