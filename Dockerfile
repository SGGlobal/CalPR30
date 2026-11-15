FROM python:3.12-slim
WORKDIR /app
COPY skippy /app/skippy
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "skippy.collaboration.railway_comment_probe"]
