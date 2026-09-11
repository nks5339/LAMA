# Target Stack Recommendation

## Detected Legacy Stack Fingerprint

- **Languages:** Python (backend), JavaScript (frontend)
- **Frameworks/Platform:** FastAPI, React 19, Tailwind CSS, CRACO
- **DB engine:** MongoDB

Evidence:
- `backend/requirements.txt:26` (`fastapi==0.110.1`)
- `frontend/package.json:47` (`"react": "^19.0.0"`)
- `frontend/package.json:92` (`"tailwindcss": "^3.4.17"`)
- `backend/db.py:3` (`AsyncIOMotorClient`) and `backend/db.py:5` (`MONGO_URL`)
- `docker-compose.yml:62` (`mongodb://127.0.0.1:27017`)

## Top-3 Target Combinations (DB pinned to legacy engine)

1. **FastAPI / Python 3.12 + MongoDB 7 + React 19 + Modular Monolith**
   - Why: lowest migration risk, preserves current Python/FastAPI velocity.
   - Backend language family: `python`

2. **Spring Boot 3.3 / Java 21 + MongoDB 7 + Angular 18 + Microservices**
   - Why: strong enterprise governance and typed contracts for larger domains.
   - Backend language family: `java`

3. **NestJS / Node 22 LTS + MongoDB 7 + Next.js 15 + Event-Driven Services (BFF)**
   - Why: fast delivery for API + UI evolution with a modern TypeScript stack.
   - Backend language family: `node`

## Hard-Constraint Check (iter-13.36)

- **(a) DB pinning:** All 3 options keep `MongoDB` (no DB swap).
- **(b) Language-family diversity:** `python`, `java`, `node` (all different).
- **(c) Distinctness:** options vary across **backend + frontend + architecture pattern**.
