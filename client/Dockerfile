# syntax=docker/dockerfile:1
# Builds index.html from the template (inlining the basemap) and serves it with nginx,
# proxying /api to the wxserver container.
FROM node:22-alpine AS build
WORKDIR /client
COPY tools tools
COPY src src
COPY data data
RUN node tools/build.js

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /client/index.html /usr/share/nginx/html/index.html
EXPOSE 80
