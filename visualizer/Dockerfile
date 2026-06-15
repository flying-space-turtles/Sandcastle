# syntax=docker/dockerfile:1.6

FROM node:22-alpine AS build

WORKDIR /app

COPY visualizer/package*.json ./visualizer/
RUN cd visualizer && npm ci

COPY visualizer ./visualizer
COPY docker-compose.yml ./docker-compose.yml
COPY config/arena.env ./config/arena.env
COPY docker/ssh/Dockerfile ./docker/ssh/Dockerfile
COPY docker/vuln/Dockerfile ./docker/vuln/Dockerfile
COPY firewall/Dockerfile ./firewall/Dockerfile
COPY services/example-vuln/Dockerfile ./services/example-vuln/Dockerfile

RUN cd visualizer && npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/visualizer/dist /usr/share/nginx/html
COPY visualizer/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
