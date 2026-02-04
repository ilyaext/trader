FROM node:20-slim

WORKDIR /app

# Install dependencies first for caching
COPY package.json ./
RUN npm install

# Copy source
COPY . .

EXPOSE 3000

# Dev mode for hot reloading
CMD ["npm", "run", "dev"]
