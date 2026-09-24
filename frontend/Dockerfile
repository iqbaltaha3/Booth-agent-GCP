FROM node:22-slim AS build

WORKDIR /app
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

COPY frontend/package*.json ./
RUN npm install

COPY frontend .
RUN npm run build

FROM python:3.11-slim

WORKDIR /app
COPY --from=build /app/out ./out

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "python -m http.server ${PORT:-8080} --directory /app/out"]
