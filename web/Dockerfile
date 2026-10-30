# syntax=docker/dockerfile:1
FROM node:26.13.0-alpine AS base

# Step 1: Dependencies installation
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

# Step 2: Build the Next.js application
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Pass build-time configuration (public API base URL and Firebase config)
ARG NEXT_PUBLIC_API_URL=""
ARG FIREBASE_API_KEY=""
ARG FIREBASE_AUTH_DOMAIN=""
ARG FIREBASE_PROJECT_ID=""
ARG FIREBASE_APP_ID=""

ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ENV FIREBASE_API_KEY=${FIREBASE_API_KEY}
ENV FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN}
ENV FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID}
ENV FIREBASE_APP_ID=${FIREBASE_APP_ID}
ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production

RUN npm run build

# Step 3: Production minimal runtime
FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# Install curl for healthcheck
RUN apk add --no-cache curl

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy public static files
COPY --from=builder /app/public ./public

# Set permissions for caching
RUN mkdir .next
RUN chown nextjs:nodejs .next

# Leverage Next.js standalone output tracing
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:3000 || exit 1

CMD ["node", "server.js"]
