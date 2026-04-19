# Используем официальный образ Python 3.11 на Ubuntu
FROM ubuntu:latest

# Устанавливаем переменные окружения для Python
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Обновляем систему и устанавливаем зависимости
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip

# Создаем рабочую директорию
WORKDIR /app

# Копируем файлы проекта
COPY server.py .
COPY settings.inf .
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip3 install --no-cache-dir -r requirements.txt

# Создаем директорию для сессий
RUN mkdir -p sessions

# Открываем порт
EXPOSE 8080

# Запускаем сервер
CMD ["python3", "server.py"]
