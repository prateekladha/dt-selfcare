# SSR web (React Router framework mode). Non-root, production deps only.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM deps AS build
COPY . .
RUN npm run build && npm prune --omit=dev

FROM node:24-alpine
ENV NODE_ENV=production PORT=3000
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/build ./build
COPY --from=build --chown=node:node /app/server.mjs ./server.mjs
USER node
EXPOSE 3000
CMD ["npm", "run", "start"]
