#!/bin/bash
set -e

# ==========================================
# 🔧 CONFIGURATION
# ==========================================
REPO_URL="https://github.com/your-username/empire_core_v2.git"   # replace with your actual repo
PROJECT_DIR="/opt/empire-orchestrator"
DOMAIN_NAME="aiapi.franciscodes.com"          # ⬅️ updated
DATE=$(date +%Y%m%d_%H%M%S)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}🚀 Deploying Empire Orchestration API to ${DOMAIN_NAME}...${NC}"

# 1. Ensure Docker network exists
if ! docker network ls | grep -q "proxy_network"; then
    echo -e "${YELLOW}⚠️  Network 'proxy_network' not found. Creating...${NC}"
    docker network create proxy_network
fi

# 2. Clone or update repo
if [ ! -d "$PROJECT_DIR" ]; then
    echo -e "${BLUE}📂 Cloning repository...${NC}"
    git clone "$REPO_URL" "$PROJECT_DIR"
    cd "$PROJECT_DIR"
else
    echo -e "${BLUE}📂 Updating repository...${NC}"
    cd "$PROJECT_DIR"
    git fetch --all
    git reset --hard origin/main
    git clean -fd
fi

# 3. Validate .env exists
if [ ! -f ".env" ]; then
    echo -e "${RED}❌ .env file missing!${NC}"
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${YELLOW}📝 Created .env from .env.example – please edit it and run again.${NC}"
        exit 1
    else
        echo -e "${RED}❌ No .env or .env.example found.${NC}"
        exit 1
    fi
fi

# 4. Build and run
echo -e "${BLUE}🔧 Stopping old containers...${NC}"
docker compose down --remove-orphans

echo -e "${BLUE}🛠️ Building new image...${NC}"
docker compose build --no-cache

echo -e "${BLUE}🚀 Starting containers...${NC}"
docker compose up -d

# 5. Health check
echo -e "${BLUE}⏳ Waiting for service to become healthy...${NC}"
for i in {1..30}; do
    if docker ps --filter "name=empire-api" --format "{{.Status}}" | grep -q "healthy"; then
        echo -e "${GREEN}✅ Service is healthy!${NC}"
        break
    elif [ $i -eq 30 ]; then
        echo -e "${RED}❌ Service did not become healthy.${NC}"
        docker logs empire-api --tail 50
        exit 1
    else
        echo -n "."
        sleep 2
    fi
done

# 6. Final instructions for Nginx Proxy Manager
echo ""
echo -e "${GREEN}🎉 Deployment complete!${NC}"
echo -e "${BLUE}📋 Next step in Nginx Proxy Manager:${NC}"
echo "1. Go to http://$(curl -s ifconfig.me):81"
echo "2. Add Proxy Host:"
echo "   - Domain: ${DOMAIN_NAME}"
echo "   - Scheme: http"
echo "   - Forward Hostname: empire-api"
echo "   - Forward Port: 8000"
echo "3. SSL: Request Let's Encrypt certificate"
echo ""
echo -e "${GREEN}🌐 Your API will be available at https://${DOMAIN_NAME}${NC}"
