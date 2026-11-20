---
name: new-api-router
description: Add a new API domain router to the Quantfolio backend following project conventions (3-file pattern)
---

Create a new API router for: {{domain}}

## The 3-file pattern

Every new domain touches exactly these 3 files:

### 1. Create `backend/app/interface/api/<domain>.py`

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.foundation.core.db import get_db

router = APIRouter(prefix="/api/<domain>", tags=["<domain>"])


@router.get("/")
def list_items(db: Session = Depends(get_db)):
    ...
```

### 2. Register in `backend/app/interface/api/__init__.py`

Append to the `routers` list:
```python
from app.interface.api.<domain> import router as <domain>_router
routers = [
    ...,
    <domain>_router,
]
```

### 3. Add response types in `frontend/src/lib/api.ts`

Export the TypeScript interface that matches the response shape:
```typescript
export interface MyDomainItem {
  id: string;
  // ...
}
```

## Conventions

- Auth: use `current_user: User = Depends(require_auth)` from `app.foundation.core.security`
- Rate limiting: use `@limiter.limit("10/minute")` for expensive endpoints
- Return 404 with `raise HTTPException(status_code=404, detail="Not found")` for missing resources
- All endpoints return Pydantic models, never raw ORM objects

## After creating

Verify the router is reachable:
```bash
cd backend && .venv/bin/uvicorn app.main:app --reload
# then: curl http://localhost:8000/api/<domain>/
```
