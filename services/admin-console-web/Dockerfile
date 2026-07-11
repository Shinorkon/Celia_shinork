FROM node:20-alpine AS build
WORKDIR /app
COPY services/admin-console-web/package.json ./
RUN npm install --frozen-lockfile 2>/dev/null || npm install
COPY services/admin-console-web/tsconfig.json services/admin-console-web/vite.config.ts services/admin-console-web/tailwind.config.js services/admin-console-web/postcss.config.js services/admin-console-web/index.html ./
COPY services/admin-console-web/src ./src
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
