# Используем легковесный базовый образ Python
FROM python:3.10-slim

# Рабочая директория
WORKDIR /app

# Копирование зависимостей и установка
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование структуры приложения
COPY ./app ./app

# Запуск Uvicorn-сервера
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]