-- Foreman index. This database is a CACHE. Everything here must be reconstructable
-- from receipts, git, GitHub, and on-disk Claude Code state.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS host (
  name TEXT PRIMARY KEY,
  os TEXT,
  role TEXT,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS project (
  slug TEXT PRIMARY KEY,
  repo TEXT,
  default_branch TEXT,
  tier_default TEXT,
  marketplace_pin TEXT,
  active INTEGER DEFAULT 1
);

-- 1. Runs and verdicts (mirrors receipts for query speed)

CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY,
  cadence TEXT NOT NULL,
  project TEXT NOT NULL,
  host TEXT NOT NULL,
  tier TEXT NOT NULL,
  started TEXT NOT NULL,
  ended TEXT,
  status TEXT NOT NULL,
  verdict TEXT,
  cost_usd REAL,
  tokens_in INTEGER,
  tokens_out INTEGER,
  cc_version TEXT,
  receipt_path TEXT
);
CREATE INDEX IF NOT EXISTS run_lookup ON run(project, cadence, started DESC);
CREATE INDEX IF NOT EXISTS run_verdict ON run(verdict, ended DESC);

CREATE TABLE IF NOT EXISTS metric (
  run_id TEXT NOT NULL,
  name TEXT NOT NULL,
  value REAL,
  text_value TEXT,
  PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS escalation (
  id INTEGER PRIMARY KEY,
  run_id TEXT,
  project TEXT NOT NULL,
  cadence TEXT NOT NULL,
  severity TEXT NOT NULL,
  summary TEXT,
  evidence TEXT,
  opened TEXT NOT NULL,
  first_seen TEXT NOT NULL,          -- start of the unbroken amber run, for aging
  github_issue TEXT,
  resolved TEXT,
  resolution TEXT
);
CREATE INDEX IF NOT EXISTS escalation_open ON escalation(resolved, severity, first_seen);

-- 2. Sessions (C1, transcripts)

CREATE TABLE IF NOT EXISTS session (
  session_uuid TEXT PRIMARY KEY,
  project TEXT,
  host TEXT,
  surface TEXT,                      -- cli | desktop | web | vscode
  cwd TEXT,
  started TEXT,
  ended TEXT,
  model TEXT,
  permission_mode TEXT,
  transcript_path TEXT,
  transcript_bytes INTEGER,
  last_offset INTEGER DEFAULT 0,     -- incremental tail cursor
  oversized INTEGER DEFAULT 0,
  cc_version TEXT,
  parser_version INTEGER
);
CREATE INDEX IF NOT EXISTS session_recent ON session(project, started DESC);

CREATE TABLE IF NOT EXISTS session_decision (
  id INTEGER PRIMARY KEY,
  session_uuid TEXT NOT NULL,
  ts TEXT,
  kind TEXT,                         -- architecture | dependency | api | scope
  summary TEXT,
  files TEXT                         -- JSON array
);

CREATE TABLE IF NOT EXISTS skill_fire (
  id INTEGER PRIMARY KEY,
  session_uuid TEXT NOT NULL,
  ts TEXT,
  skill TEXT,
  scope TEXT,                        -- user | project | plugin
  source TEXT,
  invoked_by TEXT                    -- model | user | schedule
);
CREATE INDEX IF NOT EXISTS skill_usage ON skill_fire(skill, ts DESC);

-- 3. Telemetry rollup (C2, OTLP)

CREATE TABLE IF NOT EXISTS telemetry_day (
  day TEXT NOT NULL,
  project TEXT NOT NULL,
  host TEXT NOT NULL,
  model TEXT NOT NULL,
  sessions INTEGER,
  cost_usd REAL,
  tokens INTEGER,
  lines_added INTEGER,
  lines_removed INTEGER,
  commits INTEGER,
  prs INTEGER,
  edit_accept INTEGER,
  edit_reject INTEGER,
  active_seconds INTEGER,
  PRIMARY KEY (day, project, host, model)
);

-- 4. Activation (C6, config resolver)

CREATE TABLE IF NOT EXISTS config_snapshot (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  host TEXT NOT NULL,
  taken TEXT NOT NULL,
  cc_version TEXT,
  hash TEXT NOT NULL,
  probe_ok INTEGER DEFAULT 0,
  effective_json TEXT
);
CREATE INDEX IF NOT EXISTS snapshot_recent ON config_snapshot(project, host, taken DESC);

CREATE TABLE IF NOT EXISTS config_key (
  snapshot_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  effective TEXT,
  winning_layer TEXT,                -- managed|cli|local|project|user|merged|probe
  shadowed TEXT,                     -- JSON array of layers
  disagrees_with_probe INTEGER DEFAULT 0,
  PRIMARY KEY (snapshot_id, key)
);

CREATE TABLE IF NOT EXISTS component (
  snapshot_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                -- plugin|skill|agent|command|hook|mcp|memory
  name TEXT NOT NULL,
  version TEXT,
  scope TEXT,                        -- user|project|plugin|managed
  source TEXT,
  invocable INTEGER,                 -- model may fire it unprompted
  always_on_tokens INTEGER,
  disagrees_with_probe INTEGER,      -- file prediction contradicted by the claude -p probe
  PRIMARY KEY (snapshot_id, kind, name, scope)
);

-- 5. Repository state (C4, C5)

CREATE TABLE IF NOT EXISTS git_state (
  project TEXT NOT NULL,
  host TEXT NOT NULL,
  taken TEXT NOT NULL,
  branch TEXT,
  ahead INTEGER,
  behind INTEGER,
  dirty_files INTEGER,
  worktrees INTEGER,
  orphan_worktrees INTEGER,
  stale_branches INTEGER,
  unmerged_agent_branches INTEGER,
  largest_blob_mb REAL,
  force_pushes_7d INTEGER,
  PRIMARY KEY (project, host, taken)
);

CREATE TABLE IF NOT EXISTS github_state (
  project TEXT NOT NULL,
  taken TEXT NOT NULL,
  open_prs INTEGER,
  agent_prs INTEGER,
  oldest_pr_days INTEGER,
  failing_checks INTEGER,
  failure_classes TEXT,              -- JSON object, class -> count
  security_alerts INTEGER,
  dependabot_open INTEGER,
  actions_minutes_month INTEGER,
  issues_open INTEGER,
  PRIMARY KEY (project, taken)
);

-- 6. Coordination

CREATE TABLE IF NOT EXISTS lock (
  key TEXT PRIMARY KEY,
  holder_host TEXT NOT NULL,
  run_id TEXT,
  acquired TEXT NOT NULL,
  expires TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota (
  taken TEXT PRIMARY KEY,
  plan TEXT,
  window_resets TEXT,
  pct_used REAL
);

CREATE TABLE IF NOT EXISTS credential (
  name TEXT PRIMARY KEY,             -- never a value, only an identifier
  kind TEXT,                         -- routine_bearer | gh_token | connector
  scope TEXT,
  expires TEXT,
  last_checked TEXT
);
