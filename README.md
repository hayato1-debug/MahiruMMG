# MahiruMMG

A responsive sakura-themed frontend for the existing Mahiru Memory Garden Render API. The application uses MongoDB/GridFS through the backend and does not use IndexedDB.

## Frontend deployment
- Publish the repository root with GitHub Pages.
- `config.js` points to the current Render service (`https://hayato2-8.onrender.com`).
- Admin page is `/admin/`.

## Backend deployment
Use `backend/` as Render Root Directory, Build Command `pip install -r requirements.txt`, Start Command `uvicorn main:app --host 0.0.0.0 --port $PORT`.
The `backend/` directory is already included in this package.

Required Render environment variables:
- `MONGODB_URI`
- `MONGODB_DB` (optional; defaults to `mahiru_memory_garden` — keep the same database name as the existing service)
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `JWT_SECRET`
- `ALLOWED_ORIGINS` (comma-separated origins; include `https://hayato1-debug.github.io` while testing the new repo, and retain the old origin until migration is verified)
- `GROQ_API_KEY` (optional, for AI chat)
- `GROQ_MODEL` (optional)
- `IDEOGRAM_API_KEY` (optional, for admin image generation)
- `MAX_UPLOAD_MB` (optional, defaults to 12)

## Important safety notes
- Do not deploy this backend over the existing Render service until a backup has been made and the API tested. The current file is a proposed replacement, not yet live-tested against your Atlas data.
- New uploads default to private. Public gallery routes only return approved public media.
- `/api/admin/export` exports metadata/settings only, not GridFS binary files; it is not a full database backup.
- Reset endpoint only supports albums, favorites, or settings and never clears media/GridFS.
- Public favorites and settings are shared between visitors in this initial version because there is no public-user account system. Do not put personal/private values in these fields.
- Private media is served only through the authenticated admin preview route. Do not share admin tokens.
