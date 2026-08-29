#!/usr/bin/env python3
import hashlib, subprocess, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
org_id = "org-" + uuid.uuid4().hex[:24]
proj_id = "proj-" + uuid.uuid4().hex[:24]
key_id = "key-" + uuid.uuid4().hex[:24]
public_key = "pk-lf-" + uuid.uuid4().hex[:32]
secret_key = "sk-lf-" + uuid.uuid4().hex[:32]
hashed = hashlib.sha256(secret_key.encode()).hexdigest()
fast_hashed = hashlib.sha256(hashed.encode()).hexdigest()
display = secret_key[:8] + "..." + secret_key[-4:]
sql = "BEGIN;\nINSERT INTO organizations (id,name,created_at,updated_at,ai_features_enabled) VALUES ('" + org_id + "','grid-qa',now(),now(),false);\nINSERT INTO projects (id,name,org_id,created_at,updated_at,has_traces) VALUES ('" + proj_id + "','grid-qa','" + org_id + "',now(),now(),false);\nINSERT INTO api_keys (id,public_key,hashed_secret_key,fast_hashed_secret_key,display_secret_key,project_id,organization_id,scope,created_at) VALUES ('" + key_id + "','" + public_key + "','" + hashed + "','" + fast_hashed + "','" + display + "','" + proj_id + "','" + org_id + "','PROJECT',now());\nCOMMIT;\n"
r = subprocess.run(["docker","exec","grid-langfuse-db","psql","-U","langfuse","-d","langfuse","-v","ON_ERROR_STOP=1"], input=sql, capture_output=True, text=True)
if r.returncode != 0:
    print("DB_INSERT_FAILED:", r.stderr[-300:]); raise SystemExit(1)
lines = ["", "# langfuse OTLP 鉴权 (langfuse_bootstrap 生成)", "LANGFUSE_PUBLIC_KEY=" + public_key, "LANGFUSE_SECRET_KEY=" + secret_key, "LANGFUSE_PROJECT_ID=" + proj_id]
with ENV.open("a", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print("OK org=" + org_id + " project=" + proj_id + " key=" + key_id)
