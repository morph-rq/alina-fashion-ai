# Local development copy

This workspace is the user's isolated development and testing app.
- Run only on 127.0.0.1:8001 unless the user requests otherwise.
- Keep all databases, uploads, reports, and backups inside this workspace.
- The sibling alina-fashion workspace serves another user through port 8000 and a Cloudflare tunnel. Do not modify, restart, migrate, or delete anything there without explicit authorization for that live app.
- AI credentials and the local SearXNG service are shared infrastructure. Avoid changing or stopping them as part of local app edits.
- Never copy local test data to the live app automatically.
